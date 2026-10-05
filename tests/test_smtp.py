"""
Tests for the SMTP server functionality.
"""

from email.message import EmailMessage
from pathlib import Path

import logging
import os

import apprise
from aiosmtpd.smtp import Envelope

from mailrise.config import MailriseConfig, TLSMode
from mailrise.router import EmailAttachment
from mailrise.simple_router import SimpleRouter
from mailrise.smtp import (
    _AttachMailrise, _describefailure, _logsafe, _parsemessage, _sanitizefilename)


def test_parsemessage() -> None:
    """Tests for email message parsing."""
    msg = EmailMessage()
    msg.set_content('Hello, World!')
    msg['From'] = ''
    msg['Subject'] = 'Test Message'
    notification = _parsemessage(msg, Envelope())
    assert notification.subject == 'Test Message'
    assert notification.body == 'Hello, World!'
    assert notification.body_format == apprise.NotifyFormat.TEXT

    msg = EmailMessage()
    msg.set_content('Hello, World!')
    msg.add_alternative('Hello, <strong>World!</strong>', subtype='html')
    notification = _parsemessage(msg, Envelope())
    assert notification.subject == '[no subject]'
    assert notification.from_ == '[no sender]'
    assert notification.body == 'Hello, <strong>World!</strong>'
    assert notification.body_format == apprise.NotifyFormat.HTML


def test_multipart() -> None:
    """Tests for email message parsing with multipart components."""
    img_name = 'bridge.jpg'
    with open(Path(__file__).parent/img_name, 'rb') as file:
        img_data = file.read()
    msg = EmailMessage()
    msg.add_related('Hello, World!')
    msg.add_related(img_data, maintype='image', subtype='jpeg')
    msg['From'] = ''
    msg['Subject'] = 'Test Message'
    notification = _parsemessage(msg, Envelope())
    assert notification.subject == 'Test Message'
    assert notification.body == 'Hello, World!'
    assert notification.body_format == apprise.NotifyFormat.TEXT

    msg = EmailMessage()
    msg.add_alternative('Hello, World!', subtype='plain')
    msg.add_alternative('<strong>Hello, World!</strong>', subtype='html')
    msg['From'] = ''
    msg['Subject'] = 'Test Message'
    notification = _parsemessage(msg, Envelope())
    assert notification.subject == 'Test Message'
    assert notification.body == '<strong>Hello, World!</strong>'
    assert notification.body_format == apprise.NotifyFormat.HTML


def test_multipart_nested_related() -> None:
    """Tests for email message parsing with nested multipart/related containing
    HTML and images."""
    img_name = 'bridge.jpg'
    with open(Path(__file__).parent/img_name, 'rb') as file:
        img_data = file.read()

    msg = EmailMessage()
    msg['From'] = 'sender@example.com'
    msg['Subject'] = 'Nested Related Test'
    msg.set_content('Plain text version')
    msg.add_alternative('<strong>HTML version with related image</strong>',
                        subtype='html')
    html_part = msg.get_body(preferencelist=('html',))
    assert html_part is not None
    html_part.add_related(
        img_data,
        maintype='image',
        subtype='jpeg',
        cid='<logo.jpg>'
    )

    notification = _parsemessage(msg, Envelope())
    assert notification.subject == 'Nested Related Test'
    assert notification.from_ == 'sender@example.com'
    assert notification.body == '<strong>HTML version with related image</strong>'
    assert notification.body_format == apprise.NotifyFormat.HTML


def test_parseattachments() -> None:
    """Tests for email message parsing with attachments."""
    img_name = 'bridge.jpg'
    with open(Path(__file__).parent/img_name, 'rb') as file:
        img_data = file.read()

    msg = EmailMessage()
    msg.set_content('Hello, World!')
    msg['From'] = 'sender@example.com'
    msg['Subject'] = 'Now With Images'
    msg.add_attachment(
        img_data,
        maintype='image',
        subtype='jpeg',
        filename=img_name
    )
    notification = _parsemessage(msg, Envelope())
    assert notification.subject == 'Now With Images'
    assert notification.from_ == 'sender@example.com'
    assert notification.body == 'Hello, World!'
    assert notification.body_format == apprise.NotifyFormat.TEXT
    assert len(notification.attachments) == 1
    assert notification.attachments[0].data == img_data
    assert notification.attachments[0].filename == img_name

    msg = EmailMessage()
    msg.set_content('Hello, World!')
    msg['From'] = 'sender@example.com'
    msg['Subject'] = 'Now With Images'
    msg.add_attachment(
        img_data,
        maintype='image',
        subtype='jpeg',
        filename=f'1_{img_name}'
    )
    msg.add_attachment(
        img_data,
        maintype='image',
        subtype='jpeg',
        filename=f'2_{img_name}'
    )
    notification = _parsemessage(msg, Envelope())
    assert notification.subject == 'Now With Images'
    assert notification.from_ == 'sender@example.com'
    assert notification.body == 'Hello, World!'
    assert notification.body_format == apprise.NotifyFormat.TEXT
    assert len(notification.attachments) == 2
    for attach in notification.attachments:
        assert attach.data == img_data
    assert notification.attachments[0].filename == f'1_{img_name}'
    assert notification.attachments[1].filename == f'2_{img_name}'


def test_sanitizefilename() -> None:
    """Tests that sender-supplied attachment filenames are made safe."""
    assert _sanitizefilename('bridge.jpg') == 'bridge.jpg'
    assert _sanitizefilename('Bericht März.pdf') == 'Bericht März.pdf'
    assert _sanitizefilename('../../etc/passwd') == 'passwd'
    assert _sanitizefilename('..\\..\\boot.ini') == 'boot.ini'
    assert _sanitizefilename('a\x00b\r\nc.txt') == 'a_b__c.txt'
    assert _sanitizefilename('..') == 'attachment'
    assert _sanitizefilename('') == ''
    long_name = _sanitizefilename('x' * 500 + '.png')
    assert len(long_name) == 200
    assert long_name.endswith('.png')

    msg = EmailMessage()
    msg.set_content('Hello, World!')
    msg.add_attachment(b'data', maintype='application', subtype='octet-stream',
                       filename='../../evil.sh')
    notification = _parsemessage(msg, Envelope())
    assert notification.attachments[0].filename == 'evil.sh'


def test_logsafe() -> None:
    """Tests that untrusted text can't forge log lines."""
    assert _logsafe('Subject\r\n[2026-01-01] CRITICAL: forged') \
        == 'Subject\\r\\n[2026-01-01] CRITICAL: forged'
    assert _logsafe('Grüße ➤ ok') == 'Grüße ➤ ok'


def test_attachment_tempfile() -> None:
    """Tests that attachment files are private and cleaned up."""
    config = MailriseConfig(
        logger=logging.getLogger(__name__),
        listen_host='',
        listen_port=8025,
        tls_mode=TLSMode.OFF,
        tls_certfile=None,
        tls_keyfile=None,
        smtp_hostname=None,
        router=SimpleRouter(senders=[]),
        authenticator=None
    )
    attach = _AttachMailrise(config, EmailAttachment(data=b'secret', filename='a.txt'))
    assert attach.download()
    path = attach.download_path
    assert path is not None
    assert attach.detected_name == 'a.txt'
    with open(path, 'rb') as file:
        assert file.read() == b'secret'
    if os.name == 'posix':
        assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700
    attach.invalidate()
    assert not os.path.exists(path)


def test_describefailure() -> None:
    """Tests the summary of failed services."""
    failed = apprise.NotifyResult(
        name='Discord', url='discord://****/',
        attempts=[apprise.result.NotifyAttempt(apprise.AppriseResultStatus.FAILURE)])
    succeeded = apprise.NotifyResult(
        name='JSON', url='json://localhost/',
        attempts=[apprise.result.NotifyAttempt(apprise.AppriseResultStatus.SUCCESS)])
    result = apprise.AppriseResult(
        status=apprise.AppriseResultStatus.PARTIAL, results=[failed, succeeded])
    assert _describefailure(result) == 'Discord <discord://****/>: FAILURE'
    assert _describefailure(apprise.AppriseResult()) == 'NOMATCH'
