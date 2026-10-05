"""
Integration tests for the SMTP server: authentication, TLS, limits, and error
handling. These run a real aiosmtpd server and talk to it with smtplib, but
notifications are captured instead of sent.
"""

import logging
import shutil
import smtplib
import socket
import ssl
import subprocess
import typing as typ
from email.message import EmailMessage as StdlibEmailMessage
from io import StringIO
from pathlib import Path

import pytest
from aiosmtpd.controller import Controller

import mailrise.smtp
from mailrise.config import MailriseConfig, load_config
from mailrise.router import AppriseNotification, Router
from mailrise.skeleton import _silence_auth_tls_warnings, controller_kwargs
from mailrise.smtp import AppriseHandler, AppriseNotifyFailure


_logger = logging.getLogger(__name__)

_CONFIG = """
configs:
  test:
    urls:
      - json://localhost
"""

_AUTH = """
smtp:
  auth:
    basic:
      user: pass
    max_failures: 3
"""


class _Server(typ.NamedTuple):
    port: int
    sent: list[AppriseNotification]


@pytest.fixture(name='notifications')
def fixture_notifications(monkeypatch: pytest.MonkeyPatch) -> list[AppriseNotification]:
    """Capture notifications instead of sending them with Apprise."""
    sent: list[AppriseNotification] = []

    async def fake_notify(_config: MailriseConfig, data: AppriseNotification) -> None:
        sent.append(data)

    monkeypatch.setattr(mailrise.smtp, '_apprise_notify', fake_notify)
    return sent


@pytest.fixture(name='serve')
def fixture_serve(notifications: list[AppriseNotification]) \
        -> typ.Iterator[typ.Callable[..., _Server]]:
    """Start Mailrise servers from YAML configurations."""
    _silence_auth_tls_warnings()
    controllers: list[Controller] = []

    def serve(yml: str, **overrides: typ.Any) -> _Server:
        config = load_config(_logger, StringIO(yml))._replace(**overrides)
        port = _free_port()
        kwargs = controller_kwargs(config)
        kwargs.update(hostname='127.0.0.1', port=port)
        controller = Controller(AppriseHandler(config=config), **kwargs)
        controller.start()
        controllers.append(controller)
        return _Server(port=port, sent=notifications)

    yield serve
    for controller in controllers:
        controller.stop()


@pytest.fixture(name='certs', scope='module')
def fixture_certs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Generate a self-signed certificate."""
    openssl = shutil.which('openssl')
    if openssl is None:
        pytest.skip('openssl is not available')
    tmp = tmp_path_factory.mktemp('certs')
    cert, key = tmp/'cert.pem', tmp/'key.pem'
    subprocess.run(
        [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
         '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
        check=True, capture_output=True)
    return cert, key


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _message(to: str = 'test@mailrise.xyz') -> StdlibEmailMessage:
    msg = StdlibEmailMessage()
    msg['From'] = 'sender@example.com'
    msg['To'] = to
    msg['Subject'] = 'Test Subject'
    msg.set_content('Hello, World!')
    return msg


def _connect(server: _Server) -> smtplib.SMTP:
    client = smtplib.SMTP('127.0.0.1', server.port, timeout=5)
    client.ehlo()
    return client


def _auth_login(client: smtplib.SMTP, user: str, password: str) -> int:
    """Perform AUTH LOGIN by hand, which smtplib only uses as a fallback, and
    return the reply code."""
    client.user, client.password = user, password
    try:
        return client.auth('LOGIN', client.auth_login, initial_response_ok=False)[0]
    except smtplib.SMTPAuthenticationError as exc:
        return exc.smtp_code


def test_no_auth(serve: typ.Callable[..., _Server]) -> None:
    """Without authentication configured, mail is accepted."""
    server = serve(_CONFIG)
    with _connect(server) as client:
        client.send_message(_message())
    assert len(server.sent) == 1
    assert server.sent[0].title == 'Test Subject (sender@example.com)'
    assert server.sent[0].body == 'Hello, World!'


def test_auth_required(serve: typ.Callable[..., _Server]) -> None:
    """Mail without authentication is refused."""
    server = serve(_CONFIG + _AUTH)
    with _connect(server) as client:
        with pytest.raises(smtplib.SMTPSenderRefused) as exc:
            client.send_message(_message())
        assert exc.value.smtp_code == 530
    assert not server.sent


def test_auth_success(serve: typ.Callable[..., _Server]) -> None:
    """Valid logins, with both mechanisms, are accepted."""
    server = serve(_CONFIG + _AUTH)
    with _connect(server) as client:
        client.login('user', 'pass')
        client.send_message(_message())
    with _connect(server) as client:
        assert _auth_login(client, 'user', 'pass') == 235
        client.send_message(_message())
    assert len(server.sent) == 2


@pytest.mark.parametrize('user,password', [('user', 'wrong'), ('nobody', 'pass')])
def test_auth_failure(serve: typ.Callable[..., _Server],
                      user: str, password: str) -> None:
    """Invalid logins get an immediate 535 reply (issue #101), with both
    mechanisms."""
    server = serve(_CONFIG + _AUTH)
    with _connect(server) as client:
        with pytest.raises(smtplib.SMTPAuthenticationError) as exc:
            client.login(user, password)
        assert exc.value.smtp_code == 535
    with _connect(server) as client:
        assert _auth_login(client, user, password) == 535


def test_auth_lockout(serve: typ.Callable[..., _Server]) -> None:
    """After too many failed logins, even valid logins are refused."""
    server = serve(_CONFIG + _AUTH)
    with _connect(server) as client:
        for _ in range(3):
            with pytest.raises(smtplib.SMTPAuthenticationError):
                client.login('user', 'wrong')
        with pytest.raises(smtplib.SMTPAuthenticationError) as exc:
            client.login('user', 'pass')
        assert exc.value.smtp_code == 454
    assert not server.sent


def test_auth_lockout_disabled(serve: typ.Callable[..., _Server]) -> None:
    """A max_failures of zero disables the lockout."""
    server = serve(_CONFIG + _AUTH.replace('max_failures: 3', 'max_failures: 0'))
    with _connect(server) as client:
        for _ in range(5):
            with pytest.raises(smtplib.SMTPAuthenticationError):
                client.login('user', 'wrong')
        client.login('user', 'pass')


def test_starttls_auth(serve: typ.Callable[..., _Server],
                       certs: tuple[Path, Path]) -> None:
    """STARTTLS and authentication work together (issue #162), and AUTH is only
    possible after STARTTLS."""
    cert, key = certs
    server = serve(_CONFIG + _AUTH + f"""
tls:
  mode: starttls
  certfile: {cert}
  keyfile: {key}
""")
    with _connect(server) as client:
        with pytest.raises(smtplib.SMTPException):
            client.login('user', 'pass')
    with _connect(server) as client:
        context = ssl.create_default_context(cafile=str(cert))
        context.check_hostname = False
        client.starttls(context=context)
        client.ehlo()
        client.login('user', 'pass')
        client.send_message(_message())
    assert len(server.sent) == 1


def test_max_recipients(serve: typ.Callable[..., _Server]) -> None:
    """Recipients over the limit are refused."""
    server = serve(_CONFIG + """
smtp:
  max_recipients: 2
""")
    with _connect(server) as client:
        assert client.mail('sender@example.com')[0] == 250
        assert client.rcpt('test@mailrise.xyz')[0] == 250
        assert client.rcpt('test@mailrise.xyz')[0] == 250
        assert client.rcpt('test@mailrise.xyz')[0] == 452


def test_data_size_limit(serve: typ.Callable[..., _Server]) -> None:
    """Messages over the size limit are refused."""
    server = serve(_CONFIG + """
smtp:
  data_size_limit: 1024
""")
    msg = _message()
    msg.set_content('x' * 4096)
    with _connect(server) as client:
        with pytest.raises((smtplib.SMTPDataError, smtplib.SMTPSenderRefused)) as exc:
            client.send_message(msg)
        assert exc.value.smtp_code == 552
    assert not server.sent


class _BrokenRouter(Router):  # pylint: disable=too-few-public-methods
    async def email_to_apprise(self, logger, email, auth_data, **kwargs):
        raise RuntimeError('secret internal detail')
        yield  # pylint: disable=unreachable


def test_router_exception(serve: typ.Callable[..., _Server]) -> None:
    """Router exceptions produce a temporary failure without internal details."""
    server = serve(_CONFIG, router=_BrokenRouter())
    with _connect(server) as client:
        with pytest.raises(smtplib.SMTPDataError) as exc:
            client.send_message(_message())
        assert exc.value.smtp_code == 451
        assert b'secret' not in exc.value.smtp_error


def test_unparseable_message(serve: typ.Callable[..., _Server],
                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Messages that can't be parsed are refused instead of crashing."""
    def unreadable(msg, _envelope):
        raise mailrise.smtp.UnreadableMultipart(msg)

    monkeypatch.setattr(mailrise.smtp, '_parsemessage', unreadable)
    server = serve(_CONFIG)
    with _connect(server) as client:
        with pytest.raises(smtplib.SMTPDataError) as exc:
            client.send_message(_message())
        assert exc.value.smtp_code == 554


def test_notify_failure(serve: typ.Callable[..., _Server],
                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Notification failures, including unexpected exceptions, are reported to
    the client."""
    for error in (AppriseNotifyFailure('Discord: FAILURE'), OSError('disk full')):
        async def failing_notify(_config, _data, error=error):
            raise error

        monkeypatch.setattr(mailrise.smtp, '_apprise_notify', failing_notify)
        server = serve(_CONFIG)
        with _connect(server) as client:
            with pytest.raises(smtplib.SMTPDataError) as exc:
                client.send_message(_message())
            assert exc.value.smtp_code == 450


def test_auth_data_passed_to_router(serve: typ.Callable[..., _Server]) -> None:
    """The authenticated username is available to routers."""
    seen: list[typ.Any] = []

    class _Router(Router):  # pylint: disable=too-few-public-methods
        async def email_to_apprise(self, logger, email, auth_data, **kwargs):
            seen.append(auth_data)
            yield AppriseNotification(config='', title='', body='')

    server = serve(_CONFIG + _AUTH, router=_Router())
    with _connect(server) as client:
        client.login('user', 'pass')
        client.send_message(_message())
    assert seen == ['user']
