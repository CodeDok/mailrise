"""
Tests for configuration loading.
"""

import logging
from io import StringIO

import pytest
import yaml
from apprise import Apprise, AppriseConfig, NotifyFormat
from pytest import MonkeyPatch

from mailrise.basic_authenticator import BasicAuthenticator
from mailrise.config import DEFAULT_DATA_SIZE_LIMIT, DEFAULT_MAX_RECIPIENTS, load_config
from mailrise.simple_router import _Key, SimpleRouter


_logger = logging.getLogger(__name__)


def test_errors() -> None:
    """Tests for :fun:`load_config`'s failure conditions."""
    with pytest.raises(SystemExit):
        file = StringIO("""
            24
        """)
        load_config(_logger, file)
    with pytest.raises(SystemExit):
        file = StringIO("""
            configs: 24
        """)
        load_config(_logger, file)
    with pytest.raises(SystemExit):
        file = StringIO("""
            configs:
              test: 24
        """)
        load_config(_logger, file)


def test_load() -> None:
    """Tests a successful load with :fun:`load_config`."""
    file = StringIO("""
        configs:
          test:
            urls:
              - json://localhost
    """)
    mrise = load_config(_logger, file)
    router = mrise.router
    assert isinstance(router, SimpleRouter)
    assert len(router.senders) == 1
    key = _Key(user='test')
    assert mrise.authenticator is None

    sender = router.get_sender(key)
    assert sender is not None
    notifier = _make_notifier(sender.config_yaml)
    assert len(notifier) == 1
    assert notifier[0].url().startswith('json://localhost/')


def test_multi_load() -> None:
    """Tests a sucessful load with :fun:`load_config` with multiple configs."""
    file = StringIO("""
        configs:
          test1:
            urls:
              - json://localhost
          test2:
            urls:
              - json://localhost
    """)
    mrise = load_config(_logger, file)
    router = mrise.router
    assert isinstance(router, SimpleRouter)
    assert len(router.senders) == 2

    for user in ('test1', 'test2'):
        key = _Key(user=user)

        sender = router.get_sender(key)
        assert sender is not None
        notifier = _make_notifier(sender.config_yaml)
        assert len(notifier) == 1
        assert notifier[0].url().startswith('json://localhost/')


def test_mailrise_options() -> None:
    """Tests a successful load with :fun:`load_config` with Mailrise-specific
    options."""
    file = StringIO("""
        configs:
          test:
            urls:
              - json://localhost
            mailrise:
              title_template: ""
              body_format: "text"
    """)
    mrise = load_config(_logger, file)
    router = mrise.router
    assert isinstance(router, SimpleRouter)
    assert len(router.senders) == 1
    key = _Key(user='test')

    sender = router.get_sender(key)
    assert sender is not None
    assert sender.title_template.template == ''
    assert sender.body_format == NotifyFormat.TEXT

    with pytest.raises(SystemExit):
        file = StringIO("""
            configs:
              test:
                urls:
                  - json://localhost
                mailrise:
                  body_format: "BAD"
        """)
        load_config(_logger, file)


def test_config_keys() -> None:
    """Tests the config key parser with both string and full email formats."""
    # Periods are allowed, except for the notification type suffixes.
    for bad_key in ('has.failure', 'has.periods.info@example.com',
                    'x.WARNING@example.com'):
        with pytest.raises(SystemExit):
            file = StringIO(f"""
                configs:
                  {bad_key}:
                    urls:
                      - json://localhost
            """)
            load_config(_logger, file)
    with pytest.raises(SystemExit):
        file = StringIO("""
            configs:
              bademail@:
                urls:
                  - json://localhost
        """)
        load_config(_logger, file)
    file = StringIO("""
        configs:
          user@example.com:
            urls:
              - json://localhost
    """)
    mrise = load_config(_logger, file)
    router = mrise.router
    assert isinstance(router, SimpleRouter)
    assert len(router.senders) == 1
    key = _Key(user='user', domain='example.com')
    assert router.get_sender(key) is not None

    file = StringIO("""
        configs:
          has.periods:
            urls:
              - json://localhost
    """)
    mrise = load_config(_logger, file)
    router = mrise.router
    assert isinstance(router, SimpleRouter)
    assert router.get_sender(_Key(user='has.periods')) is not None


def test_fnmatch_config_keys() -> None:
    """Tests the config key parser with fnmatch pattern tokens."""
    # This defaults to "*@mailrise.xyz", which may not be obvious at first
    # glance.
    file = StringIO("""
        configs:
          "*":
            urls:
              - json://localhost
    """)
    mrise = load_config(_logger, file)
    router = mrise.router
    assert isinstance(router, SimpleRouter)
    key = _Key(user='user', domain='example.com')
    assert router.get_sender(key) is None
    key = _Key(user='user', domain='mailrise.xyz')
    assert router.get_sender(key) is not None

    file = StringIO("""
        configs:
          "*@*":
            urls:
              - json://localhost
    """)
    mrise = load_config(_logger, file)
    router = mrise.router
    assert isinstance(router, SimpleRouter)
    key = _Key(user='user', domain='example.com')
    assert router.get_sender(key) is not None

    file = StringIO("""
        configs:
          "the*@*":
            urls:
              - json://localhost
    """)
    mrise = load_config(_logger, file)
    router = mrise.router
    assert isinstance(router, SimpleRouter)
    key = _Key(user='user', domain='example.com')
    assert router.get_sender(key) is None
    key = _Key(user='thequickbrownfox', domain='example.com')
    assert router.get_sender(key) is not None


def test_authenticator() -> None:
    """Tests a successful load with an authenticator."""
    file = StringIO("""
        configs:
          test:
            urls:
              - json://localhost
        smtp:
          auth:
            basic:
              username: password
              AzureDiamond: hunter2
    """)
    mrise = load_config(_logger, file)
    assert isinstance(mrise.authenticator, BasicAuthenticator)
    logins = mrise.authenticator.logins
    assert logins['username'] == 'password'
    assert logins['AzureDiamond'] == 'hunter2'
    assert 'test' not in logins


def test_env_var() -> None:
    """Tests the environment variable loader."""
    with MonkeyPatch.context() as ctx:
        ctx.setenv('mytesturl', 'json://localhost')

        files = [
            StringIO("""
              configs:
                test:
                  urls:
                    - !env_var mytesturl
            """),
            StringIO("""
              configs:
                test:
                  urls:
                    - !env_var fallback json://localhost
            """)
        ]
        for file in files:
            mrise = load_config(_logger, file)
            router = mrise.router
            assert isinstance(router, SimpleRouter)
            assert len(router.senders) == 1
            key = _Key(user='test')
            sender = router.get_sender(key)
            assert sender is not None
            notifier = _make_notifier(sender.config_yaml)
            ap_configs = notifier.services
            assert len(ap_configs) == 1
            config = ap_configs[0]
            assert isinstance(config, AppriseConfig)
            services = config.services()
            assert len(services) == 1
            assert services[0].url().startswith('json://localhost')

    with pytest.raises(SystemExit):
        file = StringIO("""
          configs:
            test:
              urls:
                - !env_var error
        """)
        load_config(_logger, file)


def test_auth_require_tls() -> None:
    """Tests that smtp.auth.require_tls refuses to start without TLS."""
    yml = """
        configs:
          test:
            urls:
              - json://localhost
        smtp:
          auth:
            basic:
              username: password
            require_tls: {require_tls}
    """
    mrise = load_config(_logger, StringIO(yml.format(require_tls='false')))
    assert mrise.authenticator is not None
    with pytest.raises(SystemExit):
        load_config(_logger, StringIO(yml.format(require_tls='true')))


def test_limits() -> None:
    """Tests the SMTP and authentication limits."""
    file = StringIO("""
        configs:
          test:
            urls:
              - json://localhost
    """)
    mrise = load_config(_logger, file)
    assert mrise.data_size_limit == DEFAULT_DATA_SIZE_LIMIT
    assert mrise.max_recipients == DEFAULT_MAX_RECIPIENTS

    file = StringIO("""
        configs:
          test:
            urls:
              - json://localhost
        smtp:
          data_size_limit: 0
          max_recipients: 5
          auth:
            basic:
              username: password
            max_failures: 10
            lockout_seconds: 60
    """)
    mrise = load_config(_logger, file)
    assert mrise.data_size_limit == 0
    assert mrise.max_recipients == 5
    assert isinstance(mrise.authenticator, BasicAuthenticator)
    assert mrise.authenticator.max_failures == 10
    assert mrise.authenticator.lockout_seconds == 60

    for bad in ('max_recipients: -1', 'data_size_limit: lots', 'max_recipients: yes'):
        with pytest.raises(SystemExit):
            load_config(_logger, StringIO(f"""
                configs:
                  test:
                    urls:
                      - json://localhost
                smtp:
                  {bad}
            """))


def test_safe_loader() -> None:
    """Tests that the configuration file cannot construct Python objects."""
    with pytest.raises(yaml.constructor.ConstructorError):
        load_config(_logger, StringIO("""
            configs:
              test:
                urls:
                  - !!python/object/apply:os.system ["true"]
        """))


def _make_notifier(config: str):
    ap_config = AppriseConfig()
    ap_config.add_config(config, format='yaml')
    return Apprise(ap_config)
