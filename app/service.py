"""Application service: every state change goes through here, then re-planning and alerts run."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncIterator, Optional

from . import alerts as alert_rules
from .clock import Clock
from .config import Settings
from .intent import IntentExtractor, redact, sanitize
from .models import ACTIVE_STATUSES, Alert, Cafe, Intent, IntentResult, Order, PlanOption, Replan, Visit, Zone
from .persistence import Persistence, Write
from .planner import (PlanError, check_replan, fmt, forward_strip, free_seats, make_options, minutes_between,
                      noise_label, noise_level, expected_occupancy, strip_state, wait_minutes)
from .seed import sample_cafe, sample_zones

log = logging.getLogger("cafe_os.service")

PROPOSAL_TTL = timedelta(minutes=15)
RECORD_TTL = timedelta(hours=24)
NO_SHOW_AFTER = timedelta(minutes=30)
TICK_EVERY = timedelta(seconds=30)


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    """The request is valid but conflicts with the current state (e.g. option just filled up)."""

    def __init__(self, message: str, options: Optional[list[PlanOption]] = None) -> None:
        super().__init__(message)
        self.options = options or []


@dataclass
class CafeState:
    cafe: Cafe
    zones: dict[str, Zone]
    visits: dict[str, Visit] = field(default_factory=dict)
    alerts: dict[str, Alert] = field(default_factory=dict)
    version: int = 0
    last_eval: Optional[datetime] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def zone_list(self) -> list[Zone]:
        return list(self.zones.values())

    @property
    def visit_list(self) -> list[Visit]:
        return list(self.visits.values())


@dataclass
class Dirty:
    cafe: bool = False
    zones: set[str] = field(default_factory=set)
    visits: set[str] = field(default_factory=set)
    deleted_visits: set[str] = field(default_factory=set)
    alerts: set[str] = field(default_factory=set)
    deleted_alerts: set[str] = field(default_factory=set)

    def writes(self, st: CafeState) -> list[Write]:
        out: list[Write] = []
        if self.cafe:
            out.append(("", st.cafe.id, st.cafe.model_dump(mode="json")))
        out += [("zones", z, st.zones[z].model_dump(mode="json")) for z in self.zones if z in st.zones]
        out += [("visits", v, st.visits[v].model_dump(mode="json")) for v in self.visits if v in st.visits]
        out += [("visits", v, None) for v in self.deleted_visits]
        out += [("alerts", a, st.alerts[a].model_dump(mode="json")) for a in self.alerts if a in st.alerts]
        out += [("alerts", a, None) for a in self.deleted_alerts]
        return out


class CafeService:
    def __init__(self, settings: Settings, persistence: Persistence, extractor: IntentExtractor) -> None:
        self.settings = settings
        self.persistence = persistence
        self.extractor = extractor
        self.states: dict[str, CafeState] = {}
        self.clocks: dict[str, Clock] = {}

    # ---------- lifecycle ----------

    async def startup(self) -> None:
        cafe_id = self.settings.cafe_id
        loaded = await self.persistence.load(cafe_id)
        if loaded:
            cafe = Cafe.model_validate(loaded["cafe"])
            st = CafeState(
                cafe=cafe,
                zones={z.id: z for z in sorted((Zone.model_validate(z) for z in loaded["zones"]),
                                               key=lambda z: (z.position, z.id))},
                visits={v["id"]: Visit.model_validate(v) for v in loaded["visits"]},
                alerts={a["id"]: Alert.model_validate(a) for a in loaded["alerts"]},
            )
            log.info("loaded cafe %s from storage: %d zones, %d visits", cafe_id, len(st.zones), len(st.visits))
        else:
            st = CafeState(cafe=sample_cafe(cafe_id), zones={z.id: z for z in sample_zones()})
            dirty = Dirty(cafe=True, zones=set(st.zones))
            await self.persistence.save(cafe_id, dirty.writes(st))
            log.info("seeded sample cafe %s", cafe_id)
        self.states[cafe_id] = st
        self.clocks[cafe_id] = Clock(st.cafe.timezone, self.settings.demo_start)

    def state(self, cafe_id: str) -> CafeState:
        st = self.states.get(cafe_id)
        if st is None:
            raise NotFound("Unknown café")
        return st

    def now(self, cafe_id: str) -> datetime:
        return self.clocks[cafe_id].now()

    @asynccontextmanager
    async def _mutate(self, cafe_id: str) -> AsyncIterator[tuple[CafeState, Dirty, datetime]]:
        st = self.state(cafe_id)
        async with st.lock:
            dirty = Dirty()
            now = self.now(cafe_id)
            try:
                yield st, dirty, now
            finally:
                # Runs even when the operation raised, so partial updates (e.g. refreshed options) still persist.
                self._reevaluate(st, dirty, now)
                st.version += 1
                await self.persistence.save(cafe_id, dirty.writes(st))

    async def tick(self, cafe_id: str) -> None:
        """Re-run time-based checks (no-shows, re-plans, alerts) at most every 30 s, triggered by polling."""
        st = self.state(cafe_id)
        now = self.now(cafe_id)
        if st.last_eval is None or now - st.last_eval >= TICK_EVERY:
            async with self._mutate(cafe_id):
                pass

    def _reevaluate(self, st: CafeState, dirty: Dirty, now: datetime) -> None:
        st.last_eval = now
        for v in list(st.visits.values()):
            expired = now > v.expires_at and v.status not in ("confirmed", "arrived", "seated")
            if expired or (v.status == "proposed" and now > v.created_at + PROPOSAL_TTL):
                del st.visits[v.id]
                dirty.deleted_visits.add(v.id)
            elif v.status == "confirmed" and now > v.plan.arrive_by + NO_SHOW_AFTER:
                self._set_status(v, "no_show", now, dirty)
        visits = st.visit_list
        for v in visits:
            if v.status == "confirmed" and v.replan is None:
                res = check_replan(st.cafe, st.zone_list, visits, v, now)
                if res:
                    v.replan = Replan(reason=res[0], options=res[1], created_at=now)
                    v.updated_at = now
                    dirty.visits.add(v.id)
                    log.info("replan offered visit=%s reason=%s", v.code, res[0])
        found = alert_rules.detect(st.cafe, st.zone_list, [v for v in visits if v.status in ACTIVE_STATUSES], now)
        merged = alert_rules.merge(st.alerts, found)
        for key in set(st.alerts) - set(merged):
            dirty.deleted_alerts.add(key)
        for key, alert in merged.items():
            if st.alerts.get(key) != alert:
                dirty.alerts.add(key)
        st.alerts = merged

    @staticmethod
    def _set_status(v: Visit, status: str, now: datetime, dirty: Dirty) -> None:
        v.status = status  # type: ignore[assignment]
        v.updated_at = now
        dirty.visits.add(v.id)

    def _visit(self, st: CafeState, visit_id: str) -> Visit:
        v = st.visits.get(visit_id)
        if v is None:
            raise NotFound("Visit not found or expired")
        return v

    def _new_code(self, st: CafeState, zone: Zone) -> str:
        used = {v.code for v in st.visits.values() if v.status not in ("done", "cancelled", "no_show")}
        letter = zone.name[0].upper()
        for _ in range(50):
            code = f"{letter}{random.randint(1, 99)}"
            if code not in used:
                return code
        return f"{letter}{secrets.token_hex(2)}"

    # ---------- customer ----------

    async def parse_intent(self, cafe_id: str, text: str) -> IntentResult:
        st = self.state(cafe_id)
        return await self.extractor.extract(text, st.cafe, self.now(cafe_id))

    async def create_plan(self, cafe_id: str, intent: Intent, text: Optional[str]) -> Visit:
        async with self._mutate(cafe_id) as (st, dirty, now):
            options = make_options(st.cafe, st.zone_list, st.visit_list, intent, now)
            if not options:
                raise PlanError("No seating zone fits that request right now. Try a shorter stay, fewer needs, or a little later.")
            best = options[0]
            visit = Visit(
                id=secrets.token_urlsafe(12),
                code=self._new_code(st, st.zones[best.zone_id]),
                status="proposed",
                created_at=now, updated_at=now, expires_at=now + RECORD_TTL,
                intent=intent, plan=best, alternatives=options[1:],
                text_redacted=redact(sanitize(text)) if text else None,
            )
            st.visits[visit.id] = visit
            dirty.visits.add(visit.id)
            return visit

    async def confirm(self, cafe_id: str, visit_id: str, option_id: str, order_item_ids: list[str]) -> Visit:
        async with self._mutate(cafe_id) as (st, dirty, now):
            v = self._visit(st, visit_id)
            if v.status != "proposed":
                raise Conflict("This visit is already confirmed or closed")
            chosen = next((o for o in [v.plan, *v.alternatives] if o.id == option_id), None)
            if chosen is None:
                raise NotFound("Unknown plan option")
            # Re-check against live conditions: the customer may have looked at the plan for a while.
            fresh = make_options(st.cafe, st.zone_list, st.visit_list, v.intent, now,
                                 arrive_override=max(chosen.arrive_by, now.replace(second=0, microsecond=0)),
                                 exclude_id=v.id, limit=6)
            current = next((o for o in fresh if o.zone_id == chosen.zone_id), None)
            if current is None:
                v.plan, v.alternatives = (fresh[0], fresh[1:3]) if fresh else (v.plan, [])
                dirty.visits.add(v.id)
                raise Conflict(f"{chosen.zone_name} just filled up. Here are updated options.", fresh[:3])
            v.plan = current
            v.alternatives = [o for o in fresh if o.zone_id != current.zone_id][:2]
            if v.code[0] != current.zone_name[0].upper():
                v.code = self._new_code(st, st.zones[current.zone_id])
            menu_ids = {m.id for m in st.cafe.menu}
            items = [i for i in dict.fromkeys(order_item_ids) if i in menu_ids][:4]
            if items and not st.cafe.policy.orders_paused:
                v.order = Order(item_ids=items, sent_at=now)
            self._set_status(v, "confirmed", now, dirty)
            log.info("visit confirmed code=%s zone=%s order=%s", v.code, current.zone_id, bool(v.order))
            return v

    async def customer_action(self, cafe_id: str, visit_id: str, action: str, minutes: int = 0,
                              feedback: Optional[str] = None) -> Visit:
        async with self._mutate(cafe_id) as (st, dirty, now):
            v = self._visit(st, visit_id)
            self._apply_visit_action(st, v, action, now, dirty, minutes, feedback)
            return v

    async def accept_replan(self, cafe_id: str, visit_id: str, option_id: str) -> Visit:
        async with self._mutate(cafe_id) as (st, dirty, now):
            v = self._visit(st, visit_id)
            if v.replan is None:
                raise Conflict("No pending update for this visit")
            option = next((o for o in v.replan.options if o.id == option_id), None)
            if option is None:
                raise NotFound("Unknown option")
            v.alternatives = [o for o in v.replan.options if o.id != option_id]
            v.plan = option
            v.plan_version += 1
            v.replan = None
            if v.code[0] != option.zone_name[0].upper():
                v.code = self._new_code(st, st.zones[option.zone_id])
            v.updated_at = now
            dirty.visits.add(v.id)
            return v

    def _apply_visit_action(self, st: CafeState, v: Visit, action: str, now: datetime, dirty: Dirty,
                            minutes: int = 0, feedback: Optional[str] = None) -> None:
        zone = st.zones.get(v.plan.zone_id)
        if action == "arrived":
            if v.status != "confirmed":
                raise Conflict("Only a confirmed visit can arrive")
            v.arrived_at = now
            v.replan = None
            self._set_status(v, "arrived", now, dirty)
        elif action == "seated":
            if v.status not in ("confirmed", "arrived"):
                raise Conflict("Visit is not on its way")
            v.seated_at = now
            v.arrived_at = v.arrived_at or now
            v.replan = None
            if zone:
                zone.occupied = min(zone.seats, zone.occupied + v.intent.party_size)
                dirty.zones.add(zone.id)
            self._set_status(v, "seated", now, dirty)
        elif action == "late":
            if v.status != "confirmed":
                raise Conflict("Only a visit that hasn't arrived can run late")
            minutes = max(5, min(60, minutes or 10))
            v.late_min += minutes
            v.plan.arrive_by += timedelta(minutes=minutes)
            res = check_replan(st.cafe, st.zone_list, st.visit_list, v, now,
                               forced_reason=f"You're running {minutes} min late. Here's your plan from {fmt(v.plan.arrive_by)}")
            v.replan = Replan(reason=res[0], options=res[1], created_at=now) if res else None
            v.updated_at = now
            dirty.visits.add(v.id)
        elif action == "keep":
            if v.replan is not None:
                current = next((o for o in v.replan.options if o.zone_id == v.plan.zone_id), None)
                if current is not None:
                    v.plan = current
                v.replan = None
                v.updated_at = now
                dirty.visits.add(v.id)
        elif action in ("leaving", "left"):
            if v.status == "seated":
                if zone:
                    zone.occupied = max(0, zone.occupied - v.intent.party_size)
                    dirty.zones.add(zone.id)
                # Calibrate how far past their declared departure guests actually stay.
                actual = max(-30.0, min(60.0, minutes_between(v.plan.leave_by, now)))
                live = st.cafe.live
                live.overstay_min = round(max(0.0, min(45.0, 0.8 * live.overstay_min + 0.2 * actual)), 1)
                dirty.cafe = True
            elif v.status not in ("arrived", "confirmed"):
                raise Conflict("Visit is not active")
            v.left_at = now
            self._set_status(v, "done", now, dirty)
        elif action == "cancel":
            if v.status not in ("proposed", "confirmed", "arrived"):
                raise Conflict("Visit can't be cancelled now")
            self._set_status(v, "cancelled", now, dirty)
        elif action == "no_show":
            if v.status != "confirmed":
                raise Conflict("Only a confirmed visit can be a no-show")
            self._set_status(v, "no_show", now, dirty)
        elif action == "feedback":
            if feedback not in ("accurate", "off"):
                raise ValueError("feedback must be accurate or off")
            v.feedback = feedback  # type: ignore[assignment]
            dirty.visits.add(v.id)
        else:
            raise ValueError(f"Unknown action {action}")

    # ---------- staff ----------

    async def staff_visit_action(self, cafe_id: str, visit_id: str, action: str) -> Visit:
        if action not in ("arrived", "seated", "left", "no_show"):
            raise ValueError("Unknown staff action")
        return await self.customer_action(cafe_id, visit_id, action)

    async def order_action(self, cafe_id: str, visit_id: str, action: str) -> Visit:
        flow = {"accept": ("sent", "accepted"), "ready": ("accepted", "ready"), "collected": ("ready", "collected")}
        if action not in flow:
            raise ValueError("Unknown order action")
        async with self._mutate(cafe_id) as (st, dirty, now):
            v = self._visit(st, visit_id)
            if v.order is None:
                raise Conflict("This visit has no order")
            before, after = flow[action]
            if v.order.status != before and not (action == "ready" and v.order.status == "sent"):
                raise Conflict(f"Order is {v.order.status}")
            v.order.status = after  # type: ignore[assignment]
            v.updated_at = now
            dirty.visits.add(v.id)
            return v

    async def zone_update(self, cafe_id: str, zone_id: str, occupied_delta: int = 0,
                          noise: Optional[int] = None) -> Zone:
        async with self._mutate(cafe_id) as (st, dirty, now):
            zone = st.zones.get(zone_id)
            if zone is None:
                raise NotFound("Unknown zone")
            if occupied_delta:
                zone.occupied = max(0, min(zone.seats, zone.occupied + occupied_delta))
            if noise is not None:
                zone.pulse_noise = noise
                zone.pulse_at = now
            dirty.zones.add(zone.id)
            return zone

    async def live_update(self, cafe_id: str, baristas_delta: int = 0, queue_delta: int = 0) -> None:
        async with self._mutate(cafe_id) as (st, dirty, now):
            live = st.cafe.live
            live.baristas = max(1, min(6, live.baristas + baristas_delta))
            live.queue_len = max(0, min(40, live.queue_len + queue_delta))
            dirty.cafe = True

    async def alert_action(self, cafe_id: str, alert_id: str, action: str) -> None:
        if action not in ("accept", "dismiss"):
            raise ValueError("action must be accept or dismiss")
        async with self._mutate(cafe_id) as (st, dirty, now):
            alert = st.alerts.get(alert_id)
            if alert is None:
                raise NotFound("Alert already cleared")
            alert.status = "accepted" if action == "accept" else "dismissed"
            dirty.alerts.add(alert.id)
            if action == "accept" and alert.action:
                verb, _, arg = alert.action.partition(":")
                if verb == "steer" and arg in st.zones:
                    st.zones[arg].steer_until = now + timedelta(minutes=45)
                    dirty.zones.add(arg)
                elif verb == "pause_orders":
                    st.cafe.policy.orders_paused = True
                    dirty.cafe = True
                elif verb == "resume_orders":
                    st.cafe.policy.orders_paused = False
                    dirty.cafe = True

    async def scenario(self, cafe_id: str, name: str) -> None:
        if not self.settings.demo_controls:
            raise PermissionError("Demo controls are disabled")
        async with self._mutate(cafe_id) as (st, dirty, now):
            live = st.cafe.live
            if name == "lunch_rush":
                live.queue_len = min(40, live.queue_len + 7)
                live.demand_multiplier = 1.8
                live.demand_until = now + timedelta(minutes=60)
                for z in st.zones.values():
                    if z.type != "booth":
                        z.occupied = min(z.seats, z.occupied + math.ceil(0.6 * (z.seats - z.occupied)))
                        dirty.zones.add(z.id)
            elif name == "barista_break":
                live.baristas = max(1, live.baristas - 1)
            elif name == "quiet_fill":
                z = next((z for z in st.zones.values() if z.type == "quiet"), None)
                if z:
                    z.occupied = z.seats
                    z.pulse_noise, z.pulse_at = 3, now
                    dirty.zones.add(z.id)
            elif name == "reset":
                fresh = sample_cafe(cafe_id)
                st.cafe = fresh
                st.zones = {z.id: z for z in sample_zones()}
                dirty.deleted_visits |= set(st.visits)
                dirty.deleted_alerts |= set(st.alerts)
                st.visits, st.alerts = {}, {}
                dirty.zones |= set(st.zones)
                self.clocks[cafe_id].reset()
                now = self.now(cafe_id)
            else:
                raise ValueError("Unknown scenario")
            dirty.cafe = True
            log.info("scenario %s applied", name)

    # ---------- read models ----------

    def version(self, cafe_id: str) -> str:
        return f"{self.state(cafe_id).version}:{self.now(cafe_id).strftime('%H%M')}"

    def pulse_view(self, cafe_id: str) -> dict:
        st = self.state(cafe_id)
        now = self.now(cafe_id)
        visits = st.visit_list
        p50, p90 = wait_minutes(st.cafe, now, now)
        zones = []
        for z in st.zone_list:
            t = now + timedelta(minutes=10)
            free = free_seats(st.cafe, z, visits, now, t)
            occ = expected_occupancy(st.cafe, z, visits, now, now)
            zones.append({
                "id": z.id, "name": z.name, "emoji": z.emoji, "type": z.type, "description": z.description,
                "seats": z.seats, "has_power": z.has_power, "call_friendly": z.call_friendly,
                "likely_free": max(0, round(free)), "state": strip_state(free, z.seats),
                "noise_label": noise_label(noise_level(z, occ, now, now)),
            })
        cafe = st.cafe
        return {
            "version": self.version(cafe_id),
            "cafe": {"id": cafe.id, "name": cafe.name, "neighbourhood": cafe.neighbourhood,
                     "open_time": cafe.policy.open_time, "close_time": cafe.policy.close_time},
            "now": fmt(now),
            "wait": {"p50": p50, "p90": p90},
            "orders_paused": cafe.policy.orders_paused,
            "holds_enabled": cafe.policy.holds_enabled,
            "menu": [m.model_dump() for m in cafe.menu],
            "zones": zones,
            "ai": self.extractor.gemini_enabled,
        }

    def visit_view(self, cafe_id: str, visit_id: str) -> dict:
        st = self.state(cafe_id)
        v = self._visit(st, visit_id)
        data = v.model_dump(mode="json", exclude={"text_redacted"})
        data["now"] = fmt(self.now(cafe_id))
        return data

    def _staff_visit(self, cafe: Cafe, v: Visit) -> dict:
        names = {m.id: f"{m.emoji} {m.name}" for m in cafe.menu}
        return {
            "id": v.id, "code": v.code, "status": v.status, "purpose": v.intent.purpose,
            "party_size": v.intent.party_size, "needs": v.intent.needs, "call_count": v.intent.call_count,
            "zone_id": v.plan.zone_id, "zone_name": v.plan.zone_name, "zone_emoji": v.plan.zone_emoji,
            "arrive_by": fmt(v.plan.arrive_by), "leave_by": fmt(v.plan.leave_by), "duration_min": v.plan.duration_min,
            "late_min": v.late_min, "replan_pending": v.replan is not None,
            "order": {"items": [names.get(i, i) for i in v.order.item_ids], "status": v.order.status} if v.order else None,
        }

    def board_view(self, cafe_id: str) -> dict:
        st = self.state(cafe_id)
        now = self.now(cafe_id)
        visits = st.visit_list
        active = [v for v in visits if v.status in ACTIVE_STATUSES]
        p50, p90 = wait_minutes(st.cafe, now, now)
        zones = []
        for z in st.zone_list:
            occ = expected_occupancy(st.cafe, z, active, now, now)
            zones.append({
                "id": z.id, "name": z.name, "emoji": z.emoji, "type": z.type, "seats": z.seats,
                "occupied": z.occupied, "has_power": z.has_power, "call_friendly": z.call_friendly,
                "noise_label": noise_label(noise_level(z, occ, now, now)),
                "pulse_noise": z.pulse_noise,
                "steering": z.steer_until is not None and now < z.steer_until,
                "planned": sum(v.intent.party_size for v in active if v.plan.zone_id == z.id and v.status != "seated"),
                "strip": forward_strip(st.cafe, z, active, now),
            })
        severity = {"critical": 0, "warn": 1, "info": 2}
        open_alerts = sorted((a for a in st.alerts.values() if a.status == "open"),
                             key=lambda a: (severity[a.severity], a.created_at))
        today = [v for v in visits if v.status not in ("proposed",)]
        cafe = st.cafe
        return {
            "version": self.version(cafe_id),
            "now": fmt(now),
            "cafe": {"id": cafe.id, "name": cafe.name, "neighbourhood": cafe.neighbourhood},
            "live": {"baristas": cafe.live.baristas, "queue_len": cafe.live.queue_len,
                     "overstay_min": cafe.live.overstay_min,
                     "rush": cafe.live.demand_until is not None and now < cafe.live.demand_until},
            "wait": {"p50": p50, "p90": p90},
            "orders_paused": cafe.policy.orders_paused,
            "zones": zones,
            "incoming": [self._staff_visit(cafe, v) for v in sorted(
                (v for v in visits if v.status in ("confirmed", "arrived")), key=lambda v: v.plan.arrive_by)],
            "seated": [self._staff_visit(cafe, v) for v in sorted(
                (v for v in visits if v.status == "seated"), key=lambda v: v.plan.leave_by)],
            "alerts": [a.model_dump(mode="json") for a in open_alerts],
            "stats": {
                "declared_today": len(today),
                "done_today": sum(1 for v in today if v.status == "done"),
                "accurate": sum(1 for v in today if v.feedback == "accurate"),
                "rated": sum(1 for v in today if v.feedback),
            },
            "demo_controls": self.settings.demo_controls,
        }
