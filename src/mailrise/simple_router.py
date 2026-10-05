"""
This is the YAML-based router for Mailrise.
"""

from email.utils import parseaddr
from fnmatch import fnmatchcase
from logging import Logger
from string import Template
import re
import typing as typ

import apprise
import yaml

from mailrise.router import AppriseNotification, EmailMessage, Router


class _Key(typ.NamedTuple):
    """An email address, or a pattern that matches email addresses.

    Attributes:
        user: The user portion of the address.
        domain: The domain portion of the address, which defaults to
            "mailrise.xyz".
    """
    user: str
    domain: str = 'mailrise.xyz'

    def __str__(self) -> str:
        return f'{self.user}@{self.domain}'

    def as_configured(self) -> str:
        """Drop the domain part of this identifier if it is 'mailrise.xyz'."""
        return self.user if self.domain == 'mailrise.xyz' else str(self)


class _Recipient(typ.NamedTuple):
    """The routing information encoded into a recipient address.

    Attributes:
        key: An index into the dictionary of senders.
        notify_type: The type of notification to send.
    """
    key: _Key
    notify_type: apprise.NotifyType


# Matches the notification type suffix of a recipient's username.
_NOTIFY_TYPE_SUFFIX = re.compile(r'(.*)\.(info|success|warning|failure)$', re.IGNORECASE)

# Matches any sender, including emails without a parseable From header.
_ANY_SENDER = _Key(user='*', domain='*')

# Stands in for a From header that isn't a parseable address. It only matches
# sender patterns that consist entirely of wildcards.
_UNKNOWN_SENDER = _Key(user='', domain='')

# Keys that mark a configs entry as an Apprise configuration, rather than a
# mapping of recipients to Apprise configurations.
_APPRISE_CONFIG_KEYS = frozenset(
    ('urls', 'include', 'mailrise', 'asset', 'tag', 'template', 'version'))


def _parsercpt(addr: str) -> _Recipient:
    _, rcpt = parseaddr(addr)
    user, domain = _parseaddrparts(rcpt)
    if not user or not domain:
        raise ValueError
    match = _NOTIFY_TYPE_SUFFIX.search(user)
    ntype = apprise.NotifyType.INFO
    if match is not None:
        user = match.group(1)
        ntypes = match.group(2).lower()
        if ntypes == 'info':
            pass
        elif ntypes == 'success':
            ntype = apprise.NotifyType.SUCCESS
        elif ntypes == 'warning':
            ntype = apprise.NotifyType.WARNING
        elif ntypes == 'failure':
            ntype = apprise.NotifyType.FAILURE
    return _Recipient(key=_Key(user=user, domain=domain.lower()), notify_type=ntype)


def _parsesender(addr: str) -> _Key:
    """Parses the From header of an email into a key. Unlike recipients,
    senders carry no notification type suffix."""
    _, sender = parseaddr(addr)
    user, domain = _parseaddrparts(sender)
    if not user or not domain:
        return _UNKNOWN_SENDER
    return _Key(user=user, domain=domain.lower())


def _parseaddrparts(email: str) -> typ.Tuple[str, str]:
    """Parses an email address into its component user and domain parts."""
    match = re.search(r'(?:"([^"@]*)"|([^@]*))@([^@]*)$', email)
    if match is None:
        return '', ''
    quoted = match.group(1) is not None
    user = match.group(1) if quoted else match.group(2)
    domain = match.group(3)
    return user, domain


class _SimpleSender(typ.NamedTuple):
    """A configured target for Apprise notifications.

    Attributes:
        config_yaml: The YAML configuration for Apprise.
        title_template: The template string for notification title texts.
        body_template: The template string for notification body texts.
        body_format: The content type for notifications. If None, this will be
            auto-detected from the body parts of emails.
    """
    config_yaml: str
    title_template: Template
    body_template: Template
    body_format: typ.Optional[apprise.NotifyFormat]


class SimpleRouter(Router):  # pylint: disable=too-few-public-methods
    """A router that uses the rules in the YAML configuration file.

    Attributes:
        senders: A list of notification targets, each with a
            [from_key, rcpt_key, sender] tuple, where from_key and rcpt_key
            contain username and domain patterns that can be matched by fnmatch
            against the email's From address and recipient address,
            respectively, and sender is the Sender instance itself.
    """
    senders: typ.List[typ.Tuple[_Key, _Key, _SimpleSender]]

    def __init__(self, senders: typ.List[typ.Tuple[_Key, _Key, _SimpleSender]]):
        super().__init__()
        self.senders = senders

    async def email_to_apprise(
        self, logger: Logger, email: EmailMessage, auth_data: typ.Any, **kwargs) \
            -> typ.AsyncGenerator[AppriseNotification, None]:
        from_key = _parsesender(email.from_)
        for addr in email.to:
            try:
                rcpt = _parsercpt(addr)
            except ValueError:
                logger.error('Not a valid Mailrise address: %s', addr)
                continue
            sender = self.get_sender(rcpt.key, from_key)
            if sender is None:
                logger.error('Recipient is not configured: %s', addr)
                continue

            mapping = {
                'subject': email.subject,
                'from': email.from_,
                'body': email.body,
                'to': str(rcpt.key),
                'config': rcpt.key.as_configured(),
                'type': rcpt.notify_type
            }
            yield AppriseNotification(
                config=sender.config_yaml,
                config_format='yaml',
                title=sender.title_template.safe_substitute(mapping),
                body=sender.body_template.safe_substitute(mapping),
                # Use the configuration body format if specified.
                body_format=sender.body_format or email.body_format,
                notify_type=rcpt.notify_type,
                attachments=email.attachments
            )

    def get_sender(self, rcpt_key: _Key, from_key: _Key = _UNKNOWN_SENDER) \
            -> _SimpleSender | None:
        """Find a sender by recipient key and, optionally, the key of the email's
        From address. Without a From key, only configs that accept any sender
        can match."""
        return next(
            (sender for (from_pattern, rcpt_pattern, sender) in self.senders
             if _matches(from_key, from_pattern) and _matches(rcpt_key, rcpt_pattern)),
            None)


def _matches(key: _Key, pattern: _Key) -> bool:
    return (fnmatchcase(key.user, pattern.user)
            and fnmatchcase(key.domain, pattern.domain))


def load_from_yaml(logger: Logger, configs_node: dict[str, typ.Any]) -> SimpleRouter:
    """Load a simple router from the YAML configs node."""
    if not isinstance(configs_node, dict):
        logger.critical('The configs node is not a YAML mapping')
        raise SystemExit(1)
    senders: typ.List[typ.Tuple[_Key, _Key, _SimpleSender]] = []
    for key, node in configs_node.items():
        if not isinstance(node, dict):
            logger.critical("YAML config node '%s' is not a mapping", key)
            raise SystemExit(1)
        if not node or _APPRISE_CONFIG_KEYS.intersection(node):
            # Direct config: recipient -> Apprise config, for any sender.
            senders.append((
                _ANY_SENDER,
                _parse_simple_key(logger, key),
                _load_simple_sender(logger, key, node)
            ))
        else:
            # Nested config: sender -> recipient -> Apprise config.
            from_key = _parse_simple_key(logger, key, default_domain='*')
            for rcpt_key, config in node.items():
                name = f'{key} ➤ {rcpt_key}'
                senders.append((
                    from_key,
                    _parse_simple_key(logger, rcpt_key),
                    _load_simple_sender(logger, name, config)
                ))
    router = SimpleRouter(senders=senders)
    if len(router.senders) < 1:
        logger.critical('No Apprise targets are configured')
        raise SystemExit(1)
    logger.info('Loaded configuration with %d recipient(s)', len(router.senders))
    return router


def _parse_simple_key(logger: Logger, key: str,
                      default_domain: str = 'mailrise.xyz') -> _Key:
    def fatal():
        logger.critical(
            "Invalid config key '%s'; should be a string or an email address "
            'whose username does not end in .info, .success, .warning, or .failure',
            key)
        raise SystemExit(1)
    if not isinstance(key, str):
        fatal()
    if '@' in key:
        user, domain = _parseaddrparts(key)
        if not user or not domain:
            fatal()
    else:
        user, domain = key, default_domain
    # These suffixes select the notification type, so they can't be part of a
    # configured username.
    if _NOTIFY_TYPE_SUFFIX.search(user):
        fatal()
    return _Key(user=user, domain=domain.lower())


def _load_simple_sender(logger: Logger, key: str, config: dict[str, typ.Any]) -> _SimpleSender:
    if not isinstance(config, dict):
        logger.critical("YAML config node '%s' is not a mapping", key)
        raise SystemExit(1)

    # Extract Mailrise-specific values.
    mr_config = config.get('mailrise', {})
    config.pop('mailrise', None)
    title_template = mr_config.get('title_template', '$subject ($from)')
    body_template = mr_config.get('body_template', '$body')
    body_format = mr_config.get('body_format', None)
    if not any(body_format == c for c in (None,
                                          apprise.NotifyFormat.TEXT,
                                          apprise.NotifyFormat.HTML,
                                          apprise.NotifyFormat.MARKDOWN)):
        logger.critical('Invalid Apprise notification format: %s', body_format)
        raise SystemExit(1)

    return _SimpleSender(
        config_yaml=yaml.safe_dump(config),
        title_template=Template(title_template),
        body_template=Template(body_template),
        body_format=body_format
    )
