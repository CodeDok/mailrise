# Mailrise: security hardening, Apprise 2.x upgrade, upstream PR merge

## Context

The fork (`CodeDok/mailrise`, `main` = upstream `60d485e`) has fallen behind: upstream is unmaintained,
Apprise shipped a breaking **v2.0.1** (2026-10-03), the Docker image carries Debian CVEs (#170), and several
auth bugs are open (#101, #102, #162). Goal: one branch that fixes the security issues, targets
`apprise>=2.0.1,<3`, and merges upstream PRs #171, #149, #154, #157 (with fixes), plus a feature roadmap.

**Verified in a scratch venv** (apprise 2.0.1, aiosmtpd 1.4.6, py3.14) with a real mailrise process + json:// sink:
- Apprise 2 works end-to-end (body + attachments). mypy is clean. Only `tests/test_config.py:250` breaks (`Apprise.servers` → `services`).
- **#101 reproduced**: wrong password → server sends nothing, client hangs until timeout. Root cause: `BasicAuthenticator` returns `AuthResult(success=False)`; `handled` defaults to `True`, so aiosmtpd assumes a reply was already sent.
- #102 (auth not enforced) **not reproducible**: unauthenticated MAIL gets `530`. #162 (STARTTLS+auth) **works** with a STARTTLS-capable client, so this is docs/client config.
- Attachment filenames such as `../../etc/x.bin` are passed through to Apprise unmodified.
- `apprise.attachment.AttachMemory` can **not** replace `_AttachMailrise`: its `.path` is not a real file, and 8 plugins (telegram, matrix, mastodon, ses, …) read `attachment.path`.

Branch: `feat/security-apprise2` off `main`.

## 1. Apprise 2 / packaging compatibility

- `setup.cfg`: `apprise>=2.0.1,<3`, `aiosmtpd>=1.4.6,<2`, `PyYAML>=6.0.1`; `python_requires = >=3.10` (apprise 2 needs >=3.9; `router.py:100` uses `Literal[...] | None` at runtime → breaks on 3.9, upstream #133). Update classifiers.
- `pyproject.toml`: build-system `setuptools>=70.1` + `setuptools_scm[toml]>=8`, drop `wheel` (fixes #160). Remove the `[[tool.mypy.overrides]] apprise.*` block (apprise 2 ships `py.typed`).
- `tests/test_config.py`: `notifier.servers` → `notifier.services` (drop the `type: ignore`).
- `src/mailrise/smtp.py` `_apprise_notify`: `async_notify` now returns `AppriseResult`. Keep the `bool()` check, but log per-service failure details from the result (status/logs) in the `Notification failed` warning. Read the exact attribute names from `apprise/result.py` while implementing.
- README: note that Apprise 2 YAML `${NAME}` template variables / `APPRISE_TEMPLATE_*` now work inside `urls`, as an alternative to `!env_var`.

## 2. Security fixes

| # | Issue | Fix | File |
|---|---|---|---|
| S1 | Bad credentials → no SMTP reply (#101) | Return `AuthResult(success=False, handled=False)` so aiosmtpd sends `535`; on success return `auth_data=username` (custom routers then get the username, and the `Session.login_data` deprecation warning goes away). Log failed logins with peer + username. | `basic_authenticator.py` |
| S2 | Timing-unsafe `==` password compare, username enumeration | `hmac.compare_digest` on UTF-8 bytes; compare against a dummy value when the user is unknown | `basic_authenticator.py` |
| S3 | Unlimited brute force | Per-peer-IP failure counter in the authenticator (sync, in-memory): after N failures within a window, reject even valid credentials until cooldown. Config `smtp.auth.max_failures` (default 5) / `lockout_seconds` (default 300). Peer comes from `session.peer`. | `basic_authenticator.py`, `config.py` |
| S4 | Cleartext AUTH with `tls: off` (#161/#162 noise) | Explicit startup `WARNING` from mailrise; suppress aiosmtpd's duplicate `UserWarning`. Opt-in strict mode `smtp.auth.require_tls: true` → `SystemExit` when TLS is off. Set `auth_require_tls` correctly for ONCONNECT. Docs: STARTTLS + auth needs a client that does STARTTLS (#162). | `config.py`, `skeleton.py`, README |
| S5 | Router exceptions leaked to client (`450 router had internal exception: {exc}`) | Generic `451 4.3.0 internal error`; `logger.exception(...)` server-side | `smtp.py` |
| S6 | `UnreadableMultipart` path falls through to an unbound `notification` → `UnboundLocalError` | Return `554 5.6.0 unable to parse message` after logging. Also merge #171 (`get_body(preferencelist=('html','plain'))` + its test). | `smtp.py`, `tests/test_smtp.py` |
| S7 | No resource limits (unauthenticated fan-out / memory) | Config `smtp.data_size_limit` (default aiosmtpd's 32 MiB, passed through) and `smtp.max_recipients` (default 100, `452 4.5.3` in `handle_RCPT`). | `config.py`, `skeleton.py`, `smtp.py` |
| S8 | `yaml.FullLoader` for config | Base `ConfigFileLoader` on `yaml.SafeLoader` (`!env_var` constructor unchanged) | `config.py` |
| S9 | Attachment temp files in shared `/tmp`, raw sender filenames | Keep `_AttachMailrise` (path needed by plugins), but write into one per-process `tempfile.mkdtemp()` dir (0700), remove it via `atexit`; sanitize `detected_name` (basename, strip control chars/separators, cap length, fallback `attachment`). Always `invalidate()` in a `finally`. | `smtp.py` |
| S10 | Docker CVEs (#170) + hardening | See §4 | `Dockerfile`, workflows |

Note on #157: sender-based routing matches the **From header, which is spoofable**. Document this in the README and recommend combining it with SMTP auth.

## 3. Upstream PRs

- **#171**: apply as-is (covered in S6).
- **#154**: `actions/checkout@v5` → `@v6` in all workflows.
- **#149**: UID/GID build args (`PUID`/`PGID`, default 1000). For TZ, use `ARG TZ=Etc/UTC` + `ENV TZ=$TZ` instead of `dpkg-reconfigure` (Python reads `TZ` + tzdata; runtime `-e TZ=` keeps working). Check during the build that tzdata exists in the slim image.
- **#157 sender routing**: merge `src/` and `tests/` changes, skip its `github-packages.yml` change, and fix these bugs before merging:
  - The sender is parsed with `_parsercpt`, so it strips `.info/.failure` suffixes and raises on `[no sender]`. Today that **drops every mail without a From header**, a regression. Use `_parseaddrparts`, and treat an unparsable sender as `('', '')` so it matches only `*@*`.
  - The "direct vs nested" heuristic misfires: use direct config iff any of `urls`, `include`, `mailrise`, `asset`, `tag`, `template`, `version` is a top-level key.
  - Nested configs are loaded with `sender_key` as the template `config` name. Pass the recipient key.
  - Fix #164 at the same time: allow dots in the user part of `user@domain` keys unless the last dot segment is a notify-type suffix (`info|success|warning|failure`). Update the `_parse_simple_key` error message.
  - Rename `rect_key` → `rcpt_key`; replace the trailing-whitespace lines.

## 4. Docker / CI (#170, #149)

- Pin `python:3.14-slim` (bookworm/trixie tag) for **both** stages; install into `/opt/venv` in the builder; copy into the runtime image **owned by root** (no `--chown`), so the runtime user cannot modify installed code.
- Runtime stage: `apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*` to pick up Debian security fixes at build time; `USER mailrise`.
- Add `.dockerignore` (`.git`, `tests`, `docs`, `.tox`, caches).
- CI: new `ci.yml` that runs pytest + mypy on py3.10–3.14 and a Trivy image scan (fail on CRITICAL/HIGH fixable), plus a weekly scheduled rebuild so base-image CVE fixes flow in. Add `.github/dependabot.yml` for `pip`, `docker`, `github-actions`.
- Docs: compose example with `read_only: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`.

## 5. Tests to add (`tests/`)

The current suite has 26% coverage on auth and none on the SMTP server. Add `tests/test_auth.py` / extend `test_smtp.py`, using `aiosmtpd.controller.Controller` on an ephemeral port + `smtplib`, with a fake router capturing notifications (no network):
- good login → 250; bad login (LOGIN and PLAIN) → 535 promptly (S1); no-auth MAIL → 530; lockout after N failures (S3).
- STARTTLS + auth with a self-signed cert generated in a fixture (`ssl`/`trustme`, or via `openssl` if available).
- `max_recipients` → 452; router exception → 451 with no exception text; unparsable multipart → 554.
- Attachment filename sanitization; temp dir is empty after a send.
- Config: `require_tls` with `tls: off` → SystemExit; SafeLoader rejects `!!python/object`.
- #157/#164 router tests (from the PR + no-From mail + dotted keys).

## 6. Docs / changelog

`CHANGELOG.rst` gets a new section (breaking: Python ≥3.10, Apprise ≥2.0.1). README: new `smtp.auth.require_tls`, `max_failures`, `lockout_seconds`, `smtp.data_size_limit`, `smtp.max_recipients`, sender routing, dotted keys, security notes.

## Verification

1. `python -m venv .venv && .venv/bin/pip install -e '.[testing]' mypy && .venv/bin/pytest && .venv/bin/mypy src`
2. Re-run the scratch e2e probe (mailrise `-vv` with `tls: off` and `tls: starttls`, json:// sink on localhost, smtplib client): good auth delivers the body + attachment; bad auth gets an immediate `535`; the 6th bad attempt is locked out; a sanitized filename arrives at the sink.
3. `docker build -t mailrise:test --build-arg PUID=1234 .`, then `docker run` with a sample config → `id` shows 1234, the container sends via the json sink; `trivy image mailrise:test` shows no fixable HIGH/CRITICAL.
4. Build sdist/wheel with a current setuptools (`python -m build`) → no `bdist_wheel` error (#160).

## Feature suggestions (not in this change, roadmap)

1. **Reject unknown recipients at `RCPT TO`** (550) via an optional `Router.accepts(rcpt)` hook. Correct SMTP semantics, and stops wasting DATA on bad addresses. Relates to #130.
2. **Hashed passwords** in `smtp.auth.basic` (`$argon2id$`/bcrypt) and `!file` secrets (Docker/K8s secrets).
3. **Per-config auth binding**: restrict which SMTP users may send to which config (uses S1's `auth_data`), which makes sender routing trustworthy.
4. **Prefer plain text / per-config body preference** (#135, #136) as `mailrise.body_preference: html|text`.
5. **Templating upgrades** (#116, #163): `${to}`/`${from}` in URLs, per-config regex find/replace on body/subject, Jinja-lite filters (truncate, strip_html).
6. **Attachment filters** (#128, #147): allow/deny by extension/MIME, max size per config, drop inline images.
7. **Health/metrics**: HTTP `/healthz` + Prometheus counters (accepted/failed/auth failures) → Docker `HEALTHCHECK`.
8. **Deduplication** (#138): drop identical messages within N seconds.
9. **Use AppriseResult** for partial-success SMTP replies, plus Apprise 2 `?retry=` / service timeouts exposed as per-config defaults.
10. **Lenient MAIL FROM params** (#98, `AUTH=<>` from vCenter) once there is integration test coverage. Implement via a narrow `smtp_MAIL` override that strips only `AUTH=` (the earlier upstream attempt broke auth because it hooked EHLO).
11. **Apprise API tags** (#159) and **cmd://** docs for Docker (#153).
