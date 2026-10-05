# first stage: build in the full image, which has git for setuptools_scm
FROM python:3.14 AS builder
WORKDIR /code
COPY . .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --no-warn-script-location . \
    && /opt/venv/bin/pip uninstall -y pip

# second stage
FROM python:3.14-slim
ARG PUID=1000
ARG PGID=1000
ARG TZ=Etc/UTC
# Pick up Debian security fixes that are newer than the base image.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    # pip isn't needed at runtime, and its vendored packages trip scanners.
    && python -m pip uninstall -y pip \
    && groupadd -r -g "${PGID}" mailrise \
    && useradd --no-log-init -r -m -u "${PUID}" -g mailrise mailrise
# Owned by root, so the mailrise user cannot modify the installed code.
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    TZ=${TZ} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER mailrise
EXPOSE 8025
ENTRYPOINT ["mailrise"]
CMD ["/etc/mailrise.conf"]
