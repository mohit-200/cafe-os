"""Runtime settings, read from environment variables only (no secrets in code)."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass

log = logging.getLogger("cafe_os.config")


@dataclass(frozen=True)
class Settings:
    app_env: str  # "dev" | "prod"
    cafe_id: str
    secret_key: str  # signs visit and staff tokens
    staff_pin: str
    storage: str  # "memory" | "postgres"
    gcp_project: str | None
    db_host: str  # hostname, or a Unix socket dir such as /cloudsql/PROJECT:REGION:INSTANCE on Cloud Run
    db_port: int
    db_name: str
    db_user: str
    db_password: str | None
    gemini_backend: str  # "off" | "vertex" | "apikey"
    gemini_model: str
    gemini_location: str
    gemini_api_key: str | None
    gemini_daily_cap: int
    demo_start: str | None  # "HH:MM": pins the café clock for a predictable demo
    demo_controls: bool


def load_settings() -> Settings:
    env = os.getenv("APP_ENV", "dev")
    secret_key = os.getenv("SECRET_KEY")
    staff_pin = os.getenv("STAFF_PIN")
    if env == "prod" and (not secret_key or not staff_pin):
        raise RuntimeError("SECRET_KEY and STAFF_PIN must be provided (from Secret Manager) in prod")
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
        log.warning("SECRET_KEY not set: using a random key, so tokens reset on restart (dev only)")
    if not staff_pin:
        staff_pin = "2468"
        log.warning("STAFF_PIN not set: using dev PIN 2468")

    return Settings(
        app_env=env,
        cafe_id=os.getenv("CAFE_ID", "bean-byte"),
        secret_key=secret_key,
        staff_pin=staff_pin,
        storage=os.getenv("STORAGE", "memory"),
        gcp_project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
        db_host=os.getenv("DB_HOST", "127.0.0.1"),
        db_port=int(os.getenv("DB_PORT", "5432")),
        db_name=os.getenv("DB_NAME", "cafeos"),
        db_user=os.getenv("DB_USER", "cafeos"),
        db_password=os.getenv("DB_PASSWORD") or None,
        gemini_backend=os.getenv("GEMINI_BACKEND", "off"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        gemini_location=os.getenv("GEMINI_LOCATION", "global"),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_daily_cap=int(os.getenv("GEMINI_DAILY_CAP", "300")),
        demo_start=os.getenv("DEMO_START", "14:30" if env == "dev" else "") or None,
        demo_controls=os.getenv("DEMO_CONTROLS", "true").lower() == "true",
    )
