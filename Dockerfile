# CAFÉ OS: one container serves the API and the static web app on Cloud Run.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY web ./web

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 cafeos
USER cafeos

ENV APP_ENV=prod PORT=8080
EXPOSE 8080

# One worker on purpose: live café state is cached in memory (see DECISIONS.md, D11).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
