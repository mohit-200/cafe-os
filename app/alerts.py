"""Staff recommendations: deterministic detectors with templated text (no LLM, so no invented advice)."""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import Alert, Cafe, Visit, Zone
from .planner import BUCKET_MIN, fmt, free_seats, minutes_between, wait_minutes

LOOKAHEAD_MIN = 30


def _best_alternative(cafe: Cafe, zone: Zone, zones: list[Zone], visits: list[Visit], now: datetime) -> Zone | None:
    candidates = [z for z in zones if z.id != zone.id and z.type != "booth" and (z.has_power or not zone.has_power)]
    if not candidates:
        return None

    def worst_free(z: Zone) -> float:
        return min(free_seats(cafe, z, visits, now, now + timedelta(minutes=m)) for m in range(0, LOOKAHEAD_MIN + 1, BUCKET_MIN))

    best = max(candidates, key=worst_free)
    return best if worst_free(best) >= 1.0 else None


def detect(cafe: Cafe, zones: list[Zone], visits: list[Visit], now: datetime) -> dict[str, Alert]:
    found: dict[str, Alert] = {}

    def add(key: str, **kw) -> None:
        found[key] = Alert(id=key, created_at=now, **kw)

    # 1. Zone about to run out of seats while planned guests are heading there.
    for z in zones:
        if z.type == "booth":
            continue
        horizon = [now + timedelta(minutes=m) for m in range(0, LOOKAHEAD_MIN + 1, BUCKET_MIN)]
        frees = [free_seats(cafe, z, visits, now, t) for t in horizon]
        short = [t for t, f in zip(horizon, frees) if f < 0.5]
        incoming = [v for v in visits if v.status == "confirmed" and v.plan.zone_id == z.id
                    and v.plan.arrive_by <= now + timedelta(minutes=LOOKAHEAD_MIN)]
        if short and (incoming or z.occupied >= z.seats):
            alt = _best_alternative(cafe, z, zones, visits, now)
            steering = z.steer_until is not None and now < z.steer_until
            planned = sum(v.intent.party_size for v in incoming)
            text = f"{z.name} looks full from {fmt(short[0])} to {fmt(short[-1] + timedelta(minutes=BUCKET_MIN))}"
            text += f", with {planned} planned arrival{'s' if planned != 1 else ''} heading there." if planned else "."
            if alt is not None:
                text += f" Suggest sending new {z.name} requests to the {alt.name}."
            add(f"shortfall:{z.id}", kind="shortfall", severity="critical" if incoming else "warn",
                title=f"{z.emoji} {z.name} about to fill up",
                text=text,
                action=None if steering else f"steer:{z.id}",
                action_label=None if steering else (f"Steer to {alt.name}" if alt else "Steer guests away"),
                data={"zone_id": z.id, "alt_zone_id": alt.id if alt else None})

    # 2. Counter wait climbing.
    p50, p90 = wait_minutes(cafe, now, now)
    if p90 >= 9:
        if cafe.policy.orders_paused:
            add("wait:high", kind="wait", severity="warn", title="☕ Counter is busy",
                text=f"The wait is about {p50}–{p90} min and phone orders are paused.",
                action="resume_orders", action_label="Resume phone orders")
        else:
            add("wait:high", kind="wait", severity="critical" if p90 >= 14 else "warn", title="☕ Counter wait is climbing",
                text=f"The predicted wait is {p50}–{p90} min. Consider a second barista on the bar, or pause phone orders until the queue clears.",
                action="pause_orders", action_label="Pause phone orders")
    elif cafe.policy.orders_paused:
        add("orders:paused", kind="wait", severity="info", title="🧾 Phone orders paused",
            text=f"The queue has eased (about {p50}–{p90} min). You can turn phone orders back on.",
            action="resume_orders", action_label="Resume phone orders")

    # 3. Tight-deadline guests whose order should jump the queue.
    for v in visits:
        if v.status in ("confirmed", "arrived") and v.order and v.order.status in ("sent", "accepted"):
            if v.plan.duration_min <= 30 or v.plan.order_now:
                names = {m.id: m.name for m in cafe.menu}
                items = ", ".join(names.get(i, i) for i in v.order.item_ids)
                add(f"priority:{v.id}", kind="priority", severity="info", title=f"⚡ {v.code}: make this order first",
                    text=f"{v.code} arrives {fmt(v.plan.arrive_by)} and has to leave by {fmt(v.plan.leave_by)}. Start the {items} first.",
                    data={"visit_id": v.id})

    # 4. Declared departures that have passed (calibration signal, gentle).
    for v in visits:
        if v.status == "seated":
            late = minutes_between(v.plan.leave_by, now)
            if late >= 20:
                add(f"overstay:{v.id}", kind="overstay", severity="info",
                    title=f"⏳ {v.code} is past their declared departure",
                    text=f"{v.code} in the {v.plan.zone_name} planned to leave at {fmt(v.plan.leave_by)} ({int(late)} min ago). No action needed. Tap 'Left' when they go so forecasts stay accurate.",
                    data={"visit_id": v.id})
    return found


def merge(existing: dict[str, Alert], found: dict[str, Alert]) -> dict[str, Alert]:
    """Keep accepted or dismissed state for alerts that are still active. Drop alerts whose condition cleared."""
    merged: dict[str, Alert] = {}
    for key, alert in found.items():
        prev = existing.get(key)
        if prev is not None:
            alert.status = prev.status
            alert.created_at = prev.created_at
        merged[key] = alert
    return merged
