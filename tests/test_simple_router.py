"""
Tests for the YAML-based router.
"""

import logging
import typing as typ
from email.message import EmailMessage as StdlibEmailMessage

import apprise
import pytest

from mailrise.router import AppriseNotification, EmailMessage
from mailrise.simple_router import (
    _Key, _parsercpt, _parsesender, _UNKNOWN_SENDER, load_from_yaml, SimpleRouter)


_logger = logging.getLogger(__name__)


def test_parsercpt() -> None:
    """Tests for recipient parsing."""
    rcpt = _parsercpt('test@mailrise.xyz')
    assert rcpt.key == _Key(user='test')
    assert rcpt.notify_type == apprise.NotifyType.INFO

    rcpt = _parsercpt('test.warning@mailrise.xyz')
    assert rcpt.key == _Key(user='test')
    assert rcpt.notify_type == apprise.NotifyType.WARNING

    rcpt = _parsercpt('"with_quotes"@mailrise.xyz')
    assert rcpt.key == _Key(user='with_quotes')
    assert rcpt.notify_type == apprise.NotifyType.INFO

    rcpt = _parsercpt('"with_quotes.success"@mailrise.xyz')
    assert rcpt.key == _Key('with_quotes')
    assert rcpt.notify_type == apprise.NotifyType.SUCCESS

    rcpt = _parsercpt('"weird_quotes".success@mailrise.xyz')
    assert rcpt.key == _Key('"weird_quotes"')
    assert rcpt.notify_type == apprise.NotifyType.SUCCESS

    rcpt = _parsercpt('John Doe <johndoe.warning@mailrise.xyz>')
    assert rcpt.key == _Key('johndoe')
    assert rcpt.notify_type == apprise.NotifyType.WARNING

    rcpt = _parsercpt('first.last.FAILURE@example.com')
    assert rcpt.key == _Key('first.last', 'example.com')
    assert rcpt.notify_type == apprise.NotifyType.FAILURE

    with pytest.raises(ValueError):
        _parsercpt("Invalid Email <bad@>")


def test_parsesender() -> None:
    """Tests for sender parsing, which keeps suffixes and tolerates garbage."""
    assert _parsesender('Some One <some.one.warning@Example.com>') \
        == _Key('some.one.warning', 'example.com')
    assert _parsesender('[no sender]') == _UNKNOWN_SENDER
    assert _parsesender('') == _UNKNOWN_SENDER


@pytest.mark.asyncio
async def test_direct_config_routes_by_recipient() -> None:
    """Tests that direct configs match the recipient address of any sender."""
    router = load_from_yaml(_logger, {
        'alerts': {'urls': ['json://localhost']}
    })
    for from_ in ('sender@example.com', '[no sender]'):
        notifications = await _route(router, from_=from_, to=['alerts@mailrise.xyz'])
        assert len(notifications) == 1
        assert notifications[0].title == f'Subject ({from_})'


@pytest.mark.asyncio
async def test_nested_config_routes_by_sender_and_recipient() -> None:
    """Tests that nested configs match both sender and recipient addresses."""
    router = load_from_yaml(_logger, {
        'sender@example.com': {
            'alerts@example.net': {'urls': ['json://localhost']}
        },
        'monitoring': {
            '*@*': {'urls': ['json://monitoring']}
        }
    })
    assert len(await _route(router, 'sender@example.com', ['alerts@example.net'])) == 1
    assert len(await _route(
        router, 'Sender <sender@EXAMPLE.com>', ['alerts@example.net'])) == 1
    assert len(await _route(router, 'other@example.com', ['alerts@example.net'])) == 0
    assert len(await _route(router, '[no sender]', ['alerts@example.net'])) == 0

    # Sender keys without a domain match any domain.
    notifications = await _route(
        router, 'monitoring@host.lan', ['anything@mailrise.xyz'])
    assert len(notifications) == 1
    assert 'json://monitoring' in notifications[0].config


@pytest.mark.asyncio
async def test_nested_and_direct_configs_in_order() -> None:
    """Tests that the first matching config wins, nested or not."""
    router = load_from_yaml(_logger, {
        'nas@*': {
            'alerts': {'urls': ['json://nas']}
        },
        'alerts': {'urls': ['json://everyone']}
    })
    nas = await _route(router, 'nas@home.lan', ['alerts@mailrise.xyz'])
    other = await _route(router, 'printer@home.lan', ['alerts@mailrise.xyz'])
    assert 'json://nas' in nas[0].config
    assert 'json://everyone' in other[0].config


@pytest.mark.asyncio
async def test_email_to_apprise_handles_multiple_recipients() -> None:
    """Tests that one email can produce notifications for multiple recipients."""
    router = load_from_yaml(_logger, {
        'alerts': {'urls': ['json://localhost']},
        'ops': {'urls': ['json://localhost']}
    })
    notifications = await _route(
        router, 'sender@example.com',
        ['alerts@mailrise.xyz', 'ops.failure@mailrise.xyz'])
    assert [n.body for n in notifications] == ['Body', 'Body']
    assert [n.notify_type for n in notifications] \
        == [apprise.NotifyType.INFO, apprise.NotifyType.FAILURE]


@pytest.mark.asyncio
async def test_dotted_config_keys() -> None:
    """Tests that usernames with periods can be configured (issue #164)."""
    router = load_from_yaml(_logger, {
        'first.last@example.com': {'urls': ['json://localhost']}
    })
    assert len(await _route(router, 'a@b.c', ['first.last@example.com'])) == 1
    assert len(await _route(router, 'a@b.c', ['first.last.warning@example.com'])) == 1
    assert len(await _route(router, 'a@b.c', ['first@example.com'])) == 0


def test_nested_config_errors() -> None:
    """Tests that malformed nested configs are rejected."""
    with pytest.raises(SystemExit):
        load_from_yaml(_logger, {'sender@example.com': {'alerts': 24}})
    with pytest.raises(SystemExit):
        load_from_yaml(_logger, {'sender@example.com': {'bad.failure': {'urls': []}}})


async def _route(router: SimpleRouter, from_: str, to: list[str]) \
        -> list[AppriseNotification]:
    email = EmailMessage(
        email_message=StdlibEmailMessage(),
        subject='Subject',
        from_=from_,
        to=to,
        body='Body',
        body_format=apprise.NotifyFormat.TEXT,
        attachments=[]
    )
    return [typ.cast(AppriseNotification, n) async for n
            in router.email_to_apprise(_logger, email, auth_data=None)]
