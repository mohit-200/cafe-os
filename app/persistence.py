"""Storage backends.

The service keeps each café's live state in memory, which is what makes polling free.
Every change is written through to Cloud SQL (PostgreSQL), and state is reloaded from it on cold start.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Protocol

log = logging.getLogger("cafe_os.persistence")

# (collection, doc_id, data). collection "" means the café record itself. data None means delete.
Write = tuple[str, str, Optional[dict]]


class Persistence(Protocol):
    async def load(self, cafe_id: str) -> Optional[dict]: ...
    async def save(self, cafe_id: str, writes: list[Write]) -> None: ...


class MemoryPersistence:
    """Local development and tests: nothing survives a restart."""

    async def load(self, cafe_id: str) -> Optional[dict]:
        return None

    async def save(self, cafe_id: str, writes: list[Write]) -> None:
        return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS cafes (
    id          text PRIMARY KEY,
    data        jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS zones (
    cafe_id     text NOT NULL REFERENCES cafes(id) ON DELETE CASCADE,
    id          text NOT NULL,
    data        jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cafe_id, id)
);
-- The Visit Ledger. Key fields are real columns for querying; the full record is in data.
CREATE TABLE IF NOT EXISTS visits (
    cafe_id     text NOT NULL REFERENCES cafes(id) ON DELETE CASCADE,
    id          text NOT NULL,
    code        text NOT NULL,
    status      text NOT NULL,
    zone_id     text NOT NULL,
    purpose     text NOT NULL,
    party_size  int  NOT NULL,
    arrive_by   timestamptz NOT NULL,
    leave_by    timestamptz NOT NULL,
    created_at  timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL,
    data        jsonb NOT NULL,
    PRIMARY KEY (cafe_id, id)
);
CREATE INDEX IF NOT EXISTS visits_by_status ON visits (cafe_id, status);
CREATE INDEX IF NOT EXISTS visits_by_expiry ON visits (expires_at);
CREATE TABLE IF NOT EXISTS alerts (
    cafe_id     text NOT NULL REFERENCES cafes(id) ON DELETE CASCADE,
    id          text NOT NULL,
    data        jsonb NOT NULL,
    PRIMARY KEY (cafe_id, id)
);
"""


class PostgresPersistence:
    """Cloud SQL for PostgreSQL. On Cloud Run, host is the /cloudsql/<connection name> socket directory."""

    def __init__(self, host: str, port: int, dbname: str, user: str, password: Optional[str]) -> None:
        from psycopg.conninfo import make_conninfo
        from psycopg_pool import ConnectionPool

        conninfo = make_conninfo(host=host, port=port, dbname=dbname, user=user, password=password or "",
                                 connect_timeout=10, application_name="cafe-os")
        # One Cloud Run instance with one worker: a tiny pool is plenty and stays far below db-f1-micro limits.
        self.pool = ConnectionPool(conninfo, min_size=1, max_size=3, open=False, check=ConnectionPool.check_connection)
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        self.pool.open(wait=True, timeout=30)
        with self.pool.connection() as conn:
            conn.execute(SCHEMA)
        self._ready = True

    def _load(self, cafe_id: str) -> Optional[dict]:
        self._ensure()
        with self.pool.connection() as conn:
            # Privacy: visit records are kept for 24 h only.
            deleted = conn.execute("DELETE FROM visits WHERE expires_at < now()").rowcount
            if deleted:
                log.info("purged %d expired visits", deleted)
            row = conn.execute("SELECT data FROM cafes WHERE id = %s", (cafe_id,)).fetchone()
            if row is None:
                return None
            return {
                "cafe": row[0],
                "zones": [r[0] for r in conn.execute("SELECT data FROM zones WHERE cafe_id = %s", (cafe_id,))],
                "visits": [r[0] for r in conn.execute("SELECT data FROM visits WHERE cafe_id = %s", (cafe_id,))],
                "alerts": [r[0] for r in conn.execute("SELECT data FROM alerts WHERE cafe_id = %s", (cafe_id,))],
            }

    def _save(self, cafe_id: str, writes: list[Write]) -> None:
        if not writes:
            return
        from psycopg.types.json import Jsonb

        self._ensure()
        with self.pool.connection() as conn, conn.transaction():
            for collection, doc_id, data in writes:
                if collection == "":
                    conn.execute(
                        "INSERT INTO cafes (id, data, updated_at) VALUES (%s, %s, now()) "
                        "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                        (cafe_id, Jsonb(data)))
                elif data is None:
                    if collection not in ("zones", "visits", "alerts"):
                        raise ValueError(collection)
                    conn.execute(f"DELETE FROM {collection} WHERE cafe_id = %s AND id = %s", (cafe_id, doc_id))
                elif collection == "zones":
                    conn.execute(
                        "INSERT INTO zones (cafe_id, id, data, updated_at) VALUES (%s, %s, %s, now()) "
                        "ON CONFLICT (cafe_id, id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                        (cafe_id, doc_id, Jsonb(data)))
                elif collection == "alerts":
                    conn.execute(
                        "INSERT INTO alerts (cafe_id, id, data) VALUES (%s, %s, %s) "
                        "ON CONFLICT (cafe_id, id) DO UPDATE SET data = EXCLUDED.data",
                        (cafe_id, doc_id, Jsonb(data)))
                elif collection == "visits":
                    plan, intent = data["plan"], data["intent"]
                    conn.execute(
                        "INSERT INTO visits (cafe_id, id, code, status, zone_id, purpose, party_size, arrive_by, "
                        "leave_by, created_at, expires_at, data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (cafe_id, id) DO UPDATE SET code = EXCLUDED.code, status = EXCLUDED.status, "
                        "zone_id = EXCLUDED.zone_id, arrive_by = EXCLUDED.arrive_by, leave_by = EXCLUDED.leave_by, "
                        "data = EXCLUDED.data",
                        (cafe_id, doc_id, data["code"], data["status"], plan["zone_id"], intent["purpose"],
                         intent["party_size"], plan["arrive_by"], plan["leave_by"], data["created_at"],
                         data["expires_at"], Jsonb(data)))
                else:
                    raise ValueError(f"unknown collection {collection}")

    async def load(self, cafe_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self._load, cafe_id)

    async def save(self, cafe_id: str, writes: list[Write]) -> None:
        await asyncio.to_thread(self._save, cafe_id, writes)


def make_persistence(settings) -> Persistence:
    if settings.storage == "postgres":
        log.info("storage=postgres host=%s db=%s user=%s", settings.db_host, settings.db_name, settings.db_user)
        return PostgresPersistence(settings.db_host, settings.db_port, settings.db_name, settings.db_user,
                                   settings.db_password)
    log.info("storage=memory")
    return MemoryPersistence()
