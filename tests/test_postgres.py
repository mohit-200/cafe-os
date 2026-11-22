"""Cloud SQL (PostgreSQL) persistence. Runs only when a test database is available:

    docker run -d --rm --name cafeos-pg -e POSTGRES_USER=cafeos -e POSTGRES_PASSWORD=localdev \
        -e POSTGRES_DB=cafeos -p 55432:5432 postgres:17-alpine
    CAFEOS_TEST_PG=1 pytest tests/test_postgres.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest

from app.config import load_settings
from app.intent import IntentExtractor
from app.models import Intent
from app.persistence import PostgresPersistence
from app.service import CafeService

pytestmark = pytest.mark.skipif(not os.getenv("CAFEOS_TEST_PG"), reason="set CAFEOS_TEST_PG=1 with a local Postgres")

CAFE = "pg-test-cafe"


def _persistence() -> PostgresPersistence:
    return PostgresPersistence(os.getenv("DB_HOST", "127.0.0.1"), int(os.getenv("DB_PORT", "55432")), "cafeos",
                               "cafeos", os.getenv("DB_PASSWORD", "localdev"))


def _service(persistence) -> CafeService:
    os.environ["CAFE_ID"] = CAFE
    try:
        settings = load_settings()
    finally:
        os.environ.pop("CAFE_ID")
    return CafeService(settings, persistence, IntentExtractor(settings))


@pytest.fixture
def pg():
    p = _persistence()
    p._ensure()
    with p.pool.connection() as conn:
        conn.execute("DELETE FROM cafes WHERE id = %s", (CAFE,))
    yield p
    with p.pool.connection() as conn:
        conn.execute("DELETE FROM cafes WHERE id = %s", (CAFE,))
    p.pool.close()


def test_state_survives_cold_start(pg):
    async def scenario():
        svc = _service(pg)
        await svc.startup()
        clock = svc.clocks[CAFE]
        clock.freeze(clock.at_hm("14:30"))
        v = await svc.create_plan(CAFE, Intent(purpose="work", duration_min=45, needs=["quiet"]), "quiet work")
        await svc.confirm(CAFE, v.id, v.plan.id, ["flat_white"])
        await svc.zone_update(CAFE, "quiet", occupied_delta=2, noise=4)
        await svc.scenario(CAFE, "lunch_rush")
        before = svc.board_view(CAFE)

        fresh = _service(_persistence())
        await fresh.startup()
        fresh.clocks[CAFE].freeze(clock.now())
        after = fresh.board_view(CAFE)
        return v, before, after, fresh

    v, before, after, fresh = asyncio.run(scenario())
    assert [x["code"] for x in after["incoming"]] == [x["code"] for x in before["incoming"]] == [v.code]
    assert after["live"] == before["live"]
    assert [z["occupied"] for z in after["zones"]] == [z["occupied"] for z in before["zones"]]
    assert {a["id"] for a in after["alerts"]} == {a["id"] for a in before["alerts"]}
    assert fresh.visit_view(CAFE, v.id)["order"]["item_ids"] == ["flat_white"]
    fresh.persistence.pool.close()


def test_visit_ledger_columns_and_expiry_purge(pg):
    async def scenario():
        svc = _service(pg)
        await svc.startup()
        clock = svc.clocks[CAFE]
        clock.freeze(clock.at_hm("14:30"))
        v = await svc.create_plan(CAFE, Intent(purpose="work", duration_min=45), None)
        await svc.confirm(CAFE, v.id, v.plan.id, [])
        return v

    v = asyncio.run(scenario())
    with pg.pool.connection() as conn:
        row = conn.execute("SELECT status, zone_id, purpose, party_size FROM visits WHERE cafe_id=%s AND id=%s",
                           (CAFE, v.id)).fetchone()
        assert row == ("confirmed", v.plan.zone_id, "work", 1)
        conn.execute("UPDATE visits SET expires_at = now() - interval '1 minute' WHERE cafe_id=%s", (CAFE,))
    asyncio.run(pg.load(CAFE))  # cold start purges expired rows
    with pg.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM visits WHERE cafe_id=%s", (CAFE,)).fetchone()[0] == 0


def test_reset_deletes_rows(pg):
    async def scenario():
        svc = _service(pg)
        await svc.startup()
        clock = svc.clocks[CAFE]
        clock.freeze(clock.at_hm("14:30"))
        v = await svc.create_plan(CAFE, Intent(purpose="work", duration_min=45), None)
        await svc.confirm(CAFE, v.id, v.plan.id, [])
        await svc.scenario(CAFE, "reset")

    asyncio.run(scenario())
    with pg.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM visits WHERE cafe_id=%s", (CAFE,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM zones WHERE cafe_id=%s", (CAFE,)).fetchone()[0] == 6
