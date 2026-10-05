"""
This is the SMTP server functionality for Mailrise.
"""

from __future__ import annotations

import asyncio
import atexit
import email.policy
import os
import re
import shutil
import typing as typ
from email import contentmanager
from email.message import EmailMessage as StdlibEmailMessage
from email.parser import BytesParser
from tempfile import NamedTemporaryFile, mkdtemp

import apprise
from aiosmtpd.smtp import Envelope, Session, SMTP
from apprise.attachment import AttachBase
from apprise.common import ContentLocation

from mailrise.config import MailriseConfig
import mailrise.router as r


class AppriseNotifyFailure(Exception):
    """Exception raised when Apprise fails to deliver a notification.

    The message summarizes which services failed.
    """


class UnreadableMultipart(Exception):
    """Exception raised for multipart messages that can't be parsed.

    Attributes:
        message: The multipart email part.
    """
    message: StdlibEmailMessage

    def __init__(self, message: StdlibEmailMessage) -> None:
        super().__init__(self)
        self.message = message


class AppriseHandler(typ.NamedTuple):
    """The aiosmtpd handler for Mailrise. Dispatches Apprise notifications.

    Attributes:
        config: This server's Mailrise configuration.
    """
    config: MailriseConfig

    # pylint: disable=invalid-name,unused-argument,too-many-arguments
    async def handle_RCPT(self, server: SMTP, session: Session, envelope: Envelope,
                          address: str, rcpt_options: list[str]) -> str:
        """Called during RCPT TO."""
        max_recipients = self.config.max_recipients
        if max_recipients and len(envelope.rcpt_tos) >= max_recipients:
            self.config.logger.warning('Rejected recipient over the limit of %d: %s',
                                       max_recipients, _logsafe(address))
            return '452 4.5.3 Too many recipients'
        self.config.logger.info('Added recipient: %s', _logsafe(address))
        envelope.rcpt_tos.append(address)
        return '250 OK'

    # pylint: disable=invalid-name,unused-argument
    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) \
            -> str:
        """Called during DATA after the entire message ('SMTP content' as described
        in RFC 5321) has been received."""
        assert isinstance(envelope.content, bytes)
        parser = BytesParser(policy=email.policy.default) # type: ignore
        message = parser.parsebytes(envelope.content)
        assert isinstance(message, StdlibEmailMessage)
        try:
            notification = _parsemessage(message, envelope)
        except UnreadableMultipart as mpe:
            subparts = \
                ' '.join(part.get_content_type() for part in mpe.message.iter_parts())
            self.config.logger.error('Failed to parse %s message: [ %s ]',
                                     mpe.message.get_content_type(), subparts)
            return '554 5.6.0 Unable to parse message'
        self.config.logger.info('Accepted email: %s', _logmessage(notification))

        try:
            to_send = [data async for data in self.config.router.email_to_apprise(
                           logger=self.config.logger,
                           email=notification,
                           auth_data=session.auth_data
                       )]
        except Exception:  # pylint: disable=broad-except
            # Don't leak internal details to the SMTP client.
            self.config.logger.exception('Router failed on email: %s',
                                         _logmessage(notification))
            return '451 4.3.0 Internal error, try again later'

        results = await asyncio.gather(
            *(_apprise_notify(self.config, data) for data in to_send),
            return_exceptions=True
        )
        failed = False
        for result in results:
            if isinstance(result, AppriseNotifyFailure):
                self.config.logger.warning('Apprise failed to notify: %s', result)
                failed = True
            elif isinstance(result, BaseException):
                self.config.logger.error('Exception while notifying', exc_info=result)
                failed = True
        if failed:
            self.config.logger.warning('Notification failed: %s', _logmessage(notification))
            return '450 4.3.0 Failed to send notification'

        return '250 OK'


def _parsemessage(msg: StdlibEmailMessage, envelope: Envelope) -> r.EmailMessage:
    """Parses an email message into an `EmailNotification`.

    Args:
        msg: The email message.

    Returns:
        The `EmailNotification` instance.
    """
    # Without "related" in the preference list, get_body() descends into
    # multipart/related containers and returns their text part.
    py_body_part = msg.get_body(preferencelist=('html', 'plain'))
    body: typ.Optional[tuple[str, apprise.NotifyFormat]]
    if isinstance(py_body_part, StdlibEmailMessage):
        body_part: StdlibEmailMessage
        try:
            py_body_part.get_content()
        except KeyError:  # stdlib failed to read the content, which means multipart
            body_part = _getmultiparttext(py_body_part)
        else:
            body_part = py_body_part
        body_content = contentmanager.raw_data_manager.get_content(body_part)
        is_html = body_part.get_content_subtype() == 'html'
        body = (body_content.strip(),
                apprise.NotifyFormat.HTML if is_html else apprise.NotifyFormat.TEXT)
    else:
        body = None
    attachments = [_parseattachment(part) for part in msg.iter_attachments()
                   if isinstance(part, StdlibEmailMessage)]
    return r.EmailMessage(
        email_message=msg,
        subject=msg.get('Subject', '[no subject]'),
        from_=msg.get('From', '[no sender]'),
        to=envelope.rcpt_tos,
        # Apprise will fail if no body is supplied.
        body=body[0] if body else '[no body]',
        body_format=body[1] if body else apprise.NotifyFormat.TEXT,
        attachments=attachments
    )


def _getmultiparttext(msg: StdlibEmailMessage) -> StdlibEmailMessage:
    """Search for the textual body part of a multipart email."""
    content_type = msg.get_content_type()
    if content_type in ('multipart/related', 'multipart/alternative'):
        parts = list(msg.iter_parts())
        # Look for these types of parts in descending order.
        for parttype in ('multipart/alternative', 'multipart/related',
                         'text/html', 'text/plain'):
            found = \
                next((p for p in parts if isinstance(p, StdlibEmailMessage)
                     and p.get_content_type() == parttype), None)
            if found is not None:
                return _getmultiparttext(found)
        raise UnreadableMultipart(msg)
    return msg


def _parseattachment(part: StdlibEmailMessage) -> r.EmailAttachment:
    return r.EmailAttachment(data=part.get_content(),
                             filename=_sanitizefilename(part.get_filename('')))


_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')
_MAX_FILENAME_LENGTH = 200


def _sanitizefilename(filename: str) -> str:
    """Make an attachment filename supplied by the sender safe for use by
    notification services: no directory components, control characters, or
    excessive length."""
    if not filename:
        return ''
    name = re.split(r'[/\\]', filename)[-1]
    name = _UNSAFE_FILENAME_CHARS.sub('_', name).strip().lstrip('.')
    if len(name) > _MAX_FILENAME_LENGTH:
        stem, ext = os.path.splitext(name)
        ext = ext[:16]
        name = stem[:_MAX_FILENAME_LENGTH - len(ext)] + ext
    return name or 'attachment'


def _logsafe(text: str) -> str:
    """Escape control characters, such as line breaks, so that untrusted text
    cannot forge log lines."""
    return ''.join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def _logmessage(msg: r.EmailMessage) -> str:
    """Abbreviate an email into one line suitable for a log message."""
    addresses = _logsafe(f'{msg.from_} ➤ {", ".join(msg.to)}')
    subject = _logsafe(msg.subject)
    body_abridged = _logsafe((msg.body.strip().split('\n')[0])[:20])
    body = f'{body_abridged} ({len(msg.body) / 1024:.1f}K)'
    attachments = ', '.join(f'{_logsafe(a.filename)} ({len(a.data) / 1024:.1f}K)'
                            for a in msg.attachments)

    attachments_field = f' attach: [ {attachments} ]' if attachments else ''
    return f'address: [ {addresses} ] subject: [ {subject} ] body: [ {body} ]{attachments_field}'


async def _apprise_notify(config: MailriseConfig, data: r.AppriseNotification):
    ap_config = apprise.AppriseConfig(asset=data.asset or r.DEFAULT_ASSET)
    ap_config.add_config(data.config, format=data.config_format)
    ap_instance = apprise.Apprise(ap_config)

    attach_base = [_AttachMailrise(config, attach) for attach in data.attachments]
    try:
        result = await ap_instance.async_notify(
            title=data.title,
            body=data.body,
            body_format=data.body_format,
            notify_type=data.notify_type,
            attach=attach_base
        )
    finally:
        # NOTE: This should probably be called by Apprise itself, but it isn't?
        for base in attach_base:
            base.invalidate()
    with result:
        if not result:
            raise AppriseNotifyFailure(_describefailure(result))


def _describefailure(result: apprise.AppriseResult) -> str:
    """Summarize which services failed, using privacy-masked URLs."""
    failures = [f'{service.name} <{service.url}>: {service.status.name}'
                for service in result
                if service.status != apprise.AppriseResultStatus.SUCCESS]
    return '; '.join(failures) if failures else result.status.name


_attachment_dir: str | None = None


def _getattachmentdir() -> str:
    """Return a private (mode 0700) temporary directory for attachment files,
    which is removed when the process exits."""
    global _attachment_dir  # pylint: disable=global-statement
    if _attachment_dir is None or not os.path.isdir(_attachment_dir):
        _attachment_dir = mkdtemp(prefix='mailrise-')
        atexit.register(shutil.rmtree, _attachment_dir, ignore_errors=True)
    return _attachment_dir


class _AttachMailrise(AttachBase):
    """An Apprise attachment type that wraps `Attachment`.

    Data is stored in temporary files for upload.

    Args:
        config: The Mailrise configuration to use.
        attach: The `Attachment` instance.
    """
    location = ContentLocation.LOCAL

    _mrfile = None  # Satisfy mypy by initializing as an Optional.

    def __init__(self, config: MailriseConfig,
                 attach: r.EmailAttachment, **kwargs: typ.Any) -> None:
        super().__init__(**kwargs)
        self._mrconfig = config
        self._mrattach = attach

    def download(self) -> bool:
        self.invalidate()

        with NamedTemporaryFile(dir=_getattachmentdir(), delete=False) as tfile:
            tfile.write(self._mrattach.data)
        self._mrfile = tfile
        self.download_path = tfile.name
        self.detected_name = self._mrattach.filename or None

        return True  # Indicates the "download" was successful.

    def invalidate(self) -> None:
        tfile = self._mrfile
        if tfile:
            try:
                os.remove(tfile.name)
            except (FileNotFoundError, OSError):
                self._mrconfig.logger.info(
                    'Failed to delete attachment file: %s', tfile.name)
            self._mrfile = None
        super().invalidate()

    def url(self, *args: typ.Any, **kwargs: typ.Any) -> str:
        return f'mailrise://{hex(id(self))}'

    @staticmethod
    def parse_url(url: str, **kwargs: typ.Any) -> typ.Dict[str, typ.Any]: # type: ignore
        return {}
