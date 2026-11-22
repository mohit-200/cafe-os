"""Shared fixtures. The café clock is frozen at 14:30 so every test is deterministic."""

from __future__ import annotations

import os
import warnings
from datetime import datetime

import pytest

warnings.filterwarnings("ignore", category=FutureWarning)

# Must be set before app.main is imported (it reads settings at import time).
os.environ.update({
    "APP_ENV": "dev",
    "SECRET_KEY": "test-secret-key-0123456789",
    "STAFF_PIN": "2468",
    "STORAGE": "memory",
    "GEMINI_BACKEND": "off",
    "DEMO_START": "14:30",
    "DEMO_CONTROLS": "true",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.clock import Clock  # noqa: E402
from app.seed import sample_cafe, sample_zones  # noqa: E402

CAFE = "bean-byte"


@pytest.fixture
def cafe():
    return sample_cafe(CAFE)


@pytest.fixture
def zones():
    return sample_zones()


@pytest.fixture
def now(cafe) -> datetime:
    return Clock(cafe.timezone).at_hm("14:30")


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        staff = staff_headers(c)
        c.post(f"/api/staff/cafes/{CAFE}/scenario", json={"name": "reset"}, headers=staff)
        clock = main.service.clocks[CAFE]
        clock.freeze(clock.at_hm("14:30"))
        main.service.states[CAFE].last_eval = None
        main._hits.clear()  # each test starts with fresh rate-limit buckets
        yield c
        clock._frozen = None
        main._hits.clear()


def staff_headers(c: TestClient) -> dict:
    token = c.post("/api/staff/login", json={"cafe_id": CAFE, "pin": "2468"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def plan(c: TestClient, text: str, **patch) -> tuple[dict, dict]:
    """Parse text, apply an optional intent patch, create a plan. Returns (visit, visit_headers)."""
    r = c.post(f"/api/cafes/{CAFE}/intent", json={"text": text})
    assert r.status_code == 200, r.text
    intent = {**r.json()["intent"], **patch}
    p = c.post(f"/api/cafes/{CAFE}/plans", json={"intent": intent, "text": text})
    assert p.status_code == 200, p.text
    body = p.json()
    return body["visit"], {"X-Visit-Token": body["token"]}


def confirm(c: TestClient, visit: dict, headers: dict, items: list[str] | None = None) -> dict:
    r = c.post(f"/api/cafes/{CAFE}/visits/{visit['id']}/confirm",
               json={"option_id": visit["plan"]["id"], "order_item_ids": items or []}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()
