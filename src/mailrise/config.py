"""
This is the YAML configuration parser for Mailrise.
"""

from __future__ import annotations

import importlib.util
import os
import typing as typ
from enum import Enum
from functools import partial
from logging import Logger
from typing import NamedTuple

import yaml
from aiosmtpd.smtp import AuthenticatorType

from mailrise.basic_authenticator import BasicAuthenticator
from mailrise.router import Router
from mailrise.simple_router import load_from_yaml as load_simple_router


class ConfigFileLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors
    """Our YAML loader class, which comes with an attached logger.

    It is based on the safe loader, so the configuration file cannot construct
    arbitrary Python objects."""
    logger: Logger

    def __init__(self, stream, logger: Logger) -> None:
        super().__init__(stream)
        self.logger = logger
        self.add_constructor('!env_var', ConfigFileLoader._env_var_constructor)

    @staticmethod
    def _env_var_constructor(loader: ConfigFileLoader, node: yaml.nodes.Node) -> str:
        """Load environment variables and embed them into the configuration YAML."""
        value = str(node.value)
        try:
            env, default = value.split(maxsplit=1)
        except ValueError:
            env, default = value, None

        if env in os.environ:
            return os.environ[env]
        if default:
            loader.logger.warning(
                'Environment variable %s not defined, using default value: %s',
                env, default)
            return default
        loader.logger.critical(
            'Environment variable %s not defined and no default value provided', env)
        raise SystemExit(1)


DEFAULT_DATA_SIZE_LIMIT = 33554432  # 32 MiB, aiosmtpd's default
DEFAULT_MAX_RECIPIENTS = 100
DEFAULT_AUTH_MAX_FAILURES = 5
DEFAULT_AUTH_LOCKOUT_SECONDS = 300


class TLSMode(Enum):
    """Specifies a TLS encryption operating mode."""
    OFF = 'no TLS'
    ONCONNECT = 'TLS on connect'
    STARTTLS = 'STARTTLS, optional'
    STARTTLSREQUIRE = 'STARTTLS, required'


class MailriseConfig(NamedTuple):
    """Configuration data for a Mailrise instance.

    Attributes:
        logger: The logger, which is used to record interesting events.
        listen_host: The network address to listen on.
        listen_port: The network port to listen on.
        tls_mode: The TLS encryption mode.
        tls_certfile: The path to the TLS certificate chain file.
        tls_keyfile: The path to the TLS key file.
        smtp_hostname: The advertised SMTP server hostname.
        router: The router that converts emails into notifications.
        authenticator: The SMTP authenticator, if authentication is enabled.
        data_size_limit: The maximum size of an email message, in bytes. Zero
            means unlimited.
        max_recipients: The maximum number of recipients per email. Zero means
            unlimited.
    """
    logger: Logger
    listen_host: str
    listen_port: int
    tls_mode: TLSMode
    tls_certfile: typ.Optional[str]
    tls_keyfile: typ.Optional[str]
    smtp_hostname: typ.Optional[str]
    router: Router
    authenticator: typ.Optional[AuthenticatorType]
    data_size_limit: int = DEFAULT_DATA_SIZE_LIMIT
    max_recipients: int = DEFAULT_MAX_RECIPIENTS


class MailriseImportedCode(NamedTuple):
    """The pluggable Python code for a Mailrise instance when imported as a
    Python module. Of course, the actual result will be a module rather than
    a named tuple, but we can expect it to share these attributes.

    Attributes:
        router: The custom router, if supplied.
        authenticator: The custom authenticator, if supplied.
    """
    router: typ.Optional[Router] = None
    authenticator: typ.Optional[AuthenticatorType] = None


def load_config(logger: Logger, file: typ.TextIO) -> MailriseConfig:
    """Loads configuration data from a YAML file.

    Args:
        logger: The logger, which will be passed to the `MailriseConfig` instance.
        file: The file handle to load YAML from.

    Returns:
        The `MailriseConfig` instance.
    """
    yml = yaml.load(
        file, Loader=partial(ConfigFileLoader, logger=logger))  # type: ignore
    if not isinstance(yml, dict):
        logger.critical('YAML root node is not a mapping')
        raise SystemExit(1)

    yml_listen = yml.get('listen', {})

    yml_tls = yml.get('tls', {})
    # "off" is a boolean value in YAML, so it will get parsed as False.
    yml_tls_mode = (yml_tls.get('mode', False) or "off").upper()
    try:
        tls_mode = TLSMode[yml_tls_mode]
    except KeyError as exc:
        logger.critical('Invalid TLS operating mode: %s', yml_tls_mode)
        raise SystemExit(1) from exc
    tls_certfile = yml_tls.get('certfile', None)
    tls_keyfile = yml_tls.get('keyfile', None)
    if tls_mode != TLSMode.OFF and not (tls_certfile and tls_keyfile):
        logger.critical('TLS enabled, but certificate and key files not specified')
        raise SystemExit(1)

    yml_smtp = yml.get('smtp', {})
    if not isinstance(yml_smtp, dict):
        logger.critical('The smtp node is not a YAML mapping')
        raise SystemExit(1)
    yml_auth = yml_smtp.get('auth', {})
    if not isinstance(yml_auth, dict):
        logger.critical('The smtp.auth node is not a YAML mapping')
        raise SystemExit(1)

    router = None
    authenticator = None
    yml_import_path = yml.get('import_code', None)
    if yml_import_path:
        logger.info('Importing configurable Python code from: %s', yml_import_path)
        imported = _load_imported_code(logger, yml_import_path)
        if imported.router:
            logger.info('Discovered a custom router')
            router = imported.router
        if imported.authenticator:
            logger.info('Discovered a custom authenticator')
            authenticator = imported.authenticator
    if not router:
        router = load_simple_router(logger, yml.get('configs', {}))
    if not authenticator:
        authenticator = _load_authenticator(logger, yml_auth)

    if authenticator is not None and tls_mode == TLSMode.OFF:
        if yml_auth.get('require_tls', False):
            logger.critical('SMTP authentication is enabled with smtp.auth.require_tls, '
                            'but TLS is off')
            raise SystemExit(1)
        logger.warning('SMTP authentication is enabled, but TLS is off. Passwords will be '
                       'sent in cleartext. Enable TLS, or set smtp.auth.require_tls to '
                       'refuse to start in this configuration.')

    return MailriseConfig(
        logger=logger,
        listen_host=yml_listen.get('host', ''),
        listen_port=yml_listen.get('port', 8025),
        tls_mode=tls_mode,
        tls_certfile=tls_certfile,
        tls_keyfile=tls_keyfile,
        smtp_hostname=yml_smtp.get('hostname', None),
        router=router,
        authenticator=authenticator,
        data_size_limit=_get_nonnegative_int(
            logger, yml_smtp, 'smtp.data_size_limit', 'data_size_limit',
            DEFAULT_DATA_SIZE_LIMIT),
        max_recipients=_get_nonnegative_int(
            logger, yml_smtp, 'smtp.max_recipients', 'max_recipients',
            DEFAULT_MAX_RECIPIENTS)
    )


def _load_imported_code(logger: Logger, file_path: str) -> MailriseImportedCode:
    spec = importlib.util.spec_from_file_location(os.path.basename(file_path), file_path)
    if not (spec and spec.loader):
        logger.critical(
            'Nonexistent path or invalid Python when importing code from: %s', file_path)
        raise SystemExit(1)

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pylint: disable=broad-except
        logger.critical('Exception when importing code from: %s', file_path, exc_info=True)
        raise SystemExit(1) from exc

    return typ.cast(MailriseImportedCode, module)


def _load_authenticator(logger: Logger, config: dict[str, typ.Any]) \
        -> typ.Optional[AuthenticatorType]:
    if 'basic' in config and isinstance(config['basic'], dict):
        logins = {str(username): str(password)
                  for username, password in config['basic'].items()}
        return typ.cast(AuthenticatorType, BasicAuthenticator(
            logins=logins,
            max_failures=_get_nonnegative_int(
                logger, config, 'smtp.auth.max_failures', 'max_failures',
                DEFAULT_AUTH_MAX_FAILURES),
            lockout_seconds=_get_nonnegative_int(
                logger, config, 'smtp.auth.lockout_seconds', 'lockout_seconds',
                DEFAULT_AUTH_LOCKOUT_SECONDS),
            logger=logger
        ))

    return None


def _get_nonnegative_int(logger: Logger, node: dict[str, typ.Any], name: str,
                         key: str, default: int) -> int:
    value = node.get(key, default)
    # bool is a subclass of int, but "yes" is not a meaningful limit.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        logger.critical('%s must be a non-negative integer, got: %r', name, value)
        raise SystemExit(1)
    return value
