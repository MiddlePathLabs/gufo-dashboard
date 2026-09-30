FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG UID=1000
ARG GID=1000
RUN groupadd --gid "${GID}" app && useradd --uid "${UID}" --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /srv
COPY pyproject.toml README.md LICENSE ./
COPY requirements.txt ./
COPY app ./app
RUN pip install --require-hashes -r requirements.txt && pip install --no-deps . \
    && mkdir -p /data && chown app:app /data

USER app
ENV DATABASE_PATH=/data/gufo-dashboard.sqlite
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('DASHBOARD_PORT','8081'), timeout=4)"
CMD ["python", "-m", "app.main"]
