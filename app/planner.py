"""Deterministic visit planner.

No AI runs here. Every seat, wait and noise number comes from café configuration, staff taps,
the demand forecast and the visits customers have declared (the "Visit Ledger").
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from .models import ACTIVE_STATUSES, Cafe, Intent, PlanItem, PlanOption, Visit, Zone

BUCKET_MIN = 5
HORIZON_MIN = 120
BAND_RANK = {"unlikely": 0, "possible": 1, "likely": 2}


class PlanError(ValueError):
    """The request can't be planned (closed, deadline already passed, and so on)."""


def fmt(t: datetime) -> str:
    return t.strftime("%H:%M")


def at_hm(hm: str, ref: datetime) -> datetime:
    hour, minute = (int(x) for x in hm.split(":"))
    return ref.replace(hour=hour, minute=minute, second=0, microsecond=0)


def ceil_minute(t: datetime) -> datetime:
    return t if (t.second == 0 and t.microsecond == 0) else t.replace(second=0, microsecond=0) + timedelta(minutes=1)


def minutes_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


# ---------- demand model ----------

def arrivals_per_min(cafe: Cafe, t: datetime) -> float:
    rate = cafe.forecast.arrivals_per_hour.get(str(t.hour), 0.0) / 60.0
    live = cafe.live
    if live.demand_until is not None and t < live.demand_until:
        rate *= live.demand_multiplier
    return rate


def overstay(cafe: Cafe) -> timedelta:
    return timedelta(minutes=max(0.0, cafe.live.overstay_min))


def zone_dwell(cafe: Cafe, zone: Zone) -> float:
    dwell = cafe.forecast.zone_dwell_min.get(zone.id, 60.0)
    return min(dwell, zone.max_stay_min) if zone.max_stay_min else dwell


def expected_occupancy(cafe: Cafe, zone: Zone, visits: Iterable[Visit], now: datetime, t: datetime,
                       exclude_id: Optional[str] = None) -> float:
    """Expected seats taken in `zone` at time `t`.

    = walk-ins seated now (leaving at the zone's typical dwell rate)
    + new walk-ins forecast to arrive and still be there
    + planned guests already seated, until their declared leave-by plus the café's overstay
    + confirmed incoming guests, from their arrival until their leave-by plus overstay
    """
    dt = max(0.0, minutes_between(now, t))
    over = overstay(cafe)
    visits = [v for v in visits if v.id != exclude_id and v.plan.zone_id == zone.id]
    seated = [v for v in visits if v.status == "seated"]
    walkins_now = max(0, zone.occupied - sum(v.intent.party_size for v in seated))
    dwell = zone_dwell(cafe, zone)

    remaining = walkins_now * math.exp(-dt / dwell)

    share = cafe.forecast.zone_share.get(zone.id, 0.0) * cafe.forecast.seat_fraction
    new = 0.0
    step = 0.0
    while step < dt:
        width = min(BUCKET_MIN, dt - step)
        mid = now + timedelta(minutes=step + width / 2)
        new += arrivals_per_min(cafe, mid) * share * width * math.exp(-minutes_between(mid, t) / dwell)
        step += width

    planned_seated = sum(v.intent.party_size for v in seated if t < v.plan.leave_by + over)
    incoming = sum(
        v.intent.party_size for v in visits
        if v.status in ("confirmed", "arrived")
        and v.plan.arrive_by - timedelta(minutes=BUCKET_MIN) <= t < v.plan.leave_by + over
    )
    return remaining + new + planned_seated + incoming


def free_seats(cafe: Cafe, zone: Zone, visits: Iterable[Visit], now: datetime, t: datetime,
               exclude_id: Optional[str] = None) -> float:
    return zone.seats - expected_occupancy(cafe, zone, visits, now, t, exclude_id)


def seat_likelihood(free: float) -> float:
    return 1.0 / (1.0 + math.exp(-1.6 * (free - 0.6)))


def band(likelihood: float) -> str:
    if likelihood >= 0.75:
        return "likely"
    if likelihood >= 0.4:
        return "possible"
    return "unlikely"


def strip_state(free: float, seats: int) -> str:
    """Thresholds scale with zone size, so a 1-seat booth isn't permanently 'tight'."""
    if free >= min(1.5, 0.6 * seats):
        return "ok"
    if free >= min(0.5, 0.3 * seats):
        return "tight"
    return "full"


def forward_strip(cafe: Cafe, zone: Zone, visits: list[Visit], now: datetime) -> list[dict]:
    out = []
    for k in range(HORIZON_MIN // BUCKET_MIN):
        t = now + timedelta(minutes=k * BUCKET_MIN)
        free = free_seats(cafe, zone, visits, now, t)
        out.append({"t": fmt(t), "free": round(free, 1), "state": strip_state(free, zone.seats)})
    return out


def wait_minutes(cafe: Cafe, now: datetime, t: datetime, prep_sec: Optional[int] = None) -> tuple[int, int]:
    """Predicted counter wait (p50, p90) in minutes for an order placed at time `t`."""
    live = cafe.live
    mu = max(1, live.baristas) * 60.0 / max(30, live.avg_prep_sec)  # orders served per minute
    dt = max(0.0, minutes_between(now, t))
    lam = arrivals_per_min(cafe, now + timedelta(minutes=dt / 2)) * cafe.forecast.order_fraction
    rho = lam / mu
    queue = live.queue_len + (lam - mu) * dt
    steady = rho / (1 - rho) if rho < 0.95 else live.queue_len + (lam - mu) * max(dt, 10)
    queue = max(queue, steady, 0.0)
    own = (prep_sec or live.avg_prep_sec) / 60.0
    p50 = queue / mu + own
    return max(1, round(p50)), max(2, math.ceil(p50 * 1.6))


def noise_level(zone: Zone, occupancy: float, now: datetime, t: datetime) -> float:
    ratio = min(1.2, occupancy / max(1, zone.seats))
    level = zone.noise_baseline + 1.6 * (ratio - 0.5)
    if zone.pulse_noise is not None and zone.pulse_at is not None:
        age = minutes_between(zone.pulse_at, t)
        weight = 0.7 * max(0.0, 1.0 - age / 45.0)
        level = weight * zone.pulse_noise + (1 - weight) * level
    return max(1.0, min(5.0, level))


def noise_label(level: float) -> str:
    if level < 2.3:
        return "calm"
    if level < 3.4:
        return "moderate"
    return "lively"


def next_declared_departure(cafe: Cafe, zone: Zone, visits: Iterable[Visit], now: datetime) -> Optional[datetime]:
    times = [v.plan.leave_by + overstay(cafe) for v in visits
             if v.status == "seated" and v.plan.zone_id == zone.id and v.plan.leave_by + overstay(cafe) > now]
    return min(times) if times else None


def planned_overlap(zone: Zone, visits: Iterable[Visit], start: datetime, end: datetime,
                    exclude_id: Optional[str]) -> int:
    return sum(
        v.intent.party_size for v in visits
        if v.id != exclude_id and v.status in ACTIVE_STATUSES and v.plan.zone_id == zone.id
        and v.plan.arrive_by < end and start < v.plan.leave_by
    )


def in_peak(cafe: Cafe, t: datetime) -> Optional[tuple[str, str]]:
    for start, end in cafe.policy.peak_windows:
        if at_hm(start, t) <= t < at_hm(end, t):
            return start, end
    return None


# ---------- planning ----------

@dataclass
class _Eval:
    free: float
    likelihood: float
    band: str
    noise: float
    noise_label: str


def _evaluate(cafe: Cafe, zone: Zone, visits: list[Visit], now: datetime, arrive: datetime,
              mid: datetime, exclude_id: Optional[str]) -> _Eval:
    free = min(free_seats(cafe, zone, visits, now, arrive, exclude_id),
               free_seats(cafe, zone, visits, now, arrive + timedelta(minutes=BUCKET_MIN), exclude_id))
    likelihood = seat_likelihood(free)
    occ_mid = expected_occupancy(cafe, zone, visits, now, mid, exclude_id) + 1
    level = noise_level(zone, occ_mid, now, mid)
    return _Eval(free, likelihood, band(likelihood), level, noise_label(level))


def make_options(cafe: Cafe, zones: list[Zone], visits: list[Visit], intent: Intent, now: datetime,
                 arrive_override: Optional[datetime] = None, exclude_id: Optional[str] = None,
                 keep_zone_id: Optional[str] = None, limit: int = 3) -> list[PlanOption]:
    """Rank seating zones for an intent and return up to `limit` plans, best first."""
    policy = cafe.policy
    arrive = arrive_override or ceil_minute(now + timedelta(minutes=5 if intent.arrive_in_min is None else intent.arrive_in_min))
    opens, closes = at_hm(policy.open_time, arrive), at_hm(policy.close_time, arrive)
    if arrive < opens or arrive >= closes - timedelta(minutes=10):
        raise PlanError(f"{cafe.name} is open {policy.open_time}–{policy.close_time}.")

    warnings: list[str] = []
    deadline = at_hm(intent.leave_by, arrive) if intent.leave_by else None
    if deadline is not None:
        available = int(minutes_between(arrive, deadline))
        if available < 10:
            raise PlanError(f"Leaving by {intent.leave_by} gives you under 10 minutes here. A takeaway might suit you better.")
        duration = intent.duration_min or available
        if duration > available:
            warnings.append(f"You asked for {duration} min but need to leave by {intent.leave_by}, so the plan gives you {available} min.")
            duration = available
    else:
        duration = intent.duration_min or (10 if intent.purpose == "quick" else 45)
    leave = arrive + timedelta(minutes=duration)
    if leave > closes:
        warnings.append(f"We close at {policy.close_time}, so the plan ends then.")
        leave, duration = closes, int(minutes_between(arrive, closes))
    mid = arrive + timedelta(minutes=duration / 2)

    menu = {m.id: m for m in cafe.menu}
    prep = max((menu[i].prep_sec for i in intent.order_item_ids if i in menu), default=None)
    wait_arrive = wait_minutes(cafe, now, arrive, prep)
    wait_now = wait_minutes(cafe, now, now, prep)

    needs = set(intent.needs)
    needs_calls = "calls" in needs or intent.call_count > 0
    group = intent.party_size > 1 or intent.purpose == "meeting" or "meeting" in needs
    need_power = "power" in needs or (intent.purpose == "work" and duration >= 60)
    booth = next((z for z in zones if z.type == "booth"), None)
    peak = in_peak(cafe, arrive)
    slack = minutes_between(leave, deadline) if deadline else 999
    order_now = bool(intent.order_item_ids) and not policy.orders_paused and (
        intent.purpose == "quick" or duration <= 30 or slack < 10 or wait_arrive[0] >= 6)

    scored: list[tuple[float, PlanOption]] = []
    for z in zones:
        if z.type == "booth" or intent.party_size > z.seats:
            continue
        if need_power and not z.has_power:
            continue
        if group and z.type == "quiet":
            continue  # conversation doesn't belong in the library zone
        if needs_calls and not z.call_friendly and booth is None:
            continue
        if z.max_stay_min and duration > z.max_stay_min:
            continue
        if z.id != keep_zone_id and planned_overlap(z, visits, arrive, leave, exclude_id) + intent.party_size > z.seats * policy.max_plannable_share:
            continue  # this zone's plannable share is used up; the rest stays walk-in only

        ev = _evaluate(cafe, z, visits, now, arrive, mid, exclude_id)
        score = 4.0 * ev.likelihood
        if "quiet" in needs:
            score += (3.4 - ev.noise) * 1.3
        if intent.purpose == "work":
            score += 0.8 if z.type in ("quiet", "work") else 0.0
        if group:
            score += 1.2 if z.type in ("lounge", "social", "outdoor") else 0.0
        if "outdoor" in needs:
            score += 2.0 if z.type == "outdoor" else 0.0
        if "comfy" in needs:
            score += 1.5 if z.type == "lounge" else 0.0
        if needs_calls:
            score += 1.5 if z.call_friendly else -0.6
        if "power" in needs and z.has_power:
            score += 0.5
        steered = z.steer_until is not None and now < z.steer_until
        if steered:
            score -= 2.5
        laptop_peak = bool(peak) and intent.purpose == "work" and duration > policy.laptop_limit_min_at_peak and z.type in ("social", "lounge")
        if laptop_peak:
            score -= 1.0

        items: list[PlanItem] = []
        n_free = max(0, math.floor(ev.free))
        if ev.band == "likely":
            items.append(PlanItem(kind="seat", icon="🪑", tier="prediction",
                                  text=f"Seat very likely free at {fmt(arrive)} (about {n_free} open)"))
        elif ev.band == "possible":
            items.append(PlanItem(kind="seat", icon="🪑", tier="prediction",
                                  text=f"Seat possibly free at {fmt(arrive)}, it'll be tight"))
        else:
            items.append(PlanItem(kind="seat", icon="🪑", tier="prediction",
                                  text=f"Seats are scarce at {fmt(arrive)}"))
        if ev.band != "likely":
            nxt = next_declared_departure(cafe, z, visits, now)
            if nxt is not None:
                items.append(PlanItem(kind="departure", icon="⏳", tier="prediction",
                                      text=f"A guest here declared they're leaving, so a seat should free up around {fmt(nxt)}"))
        if policy.holds_enabled and ev.band == "likely":
            items.append(PlanItem(kind="hold", icon="✅", tier="commitment",
                                  text=f"The café holds a seat for {policy.hold_minutes} min after {fmt(arrive)} once you confirm"))
        items.append(PlanItem(kind="arrive", icon="🚶", tier="recommendation", text=f"Arrive by {fmt(arrive)}"))
        if policy.orders_paused:
            items.append(PlanItem(kind="order", icon="🧾", tier="policy",
                                  text="Phone orders are paused right now, please order at the counter"))
        elif order_now:
            ready = max(now + timedelta(minutes=wait_now[0]), arrive)
            items.append(PlanItem(kind="order", icon="⚡", tier="recommendation",
                                  text=f"Send your order now so it's ready around {fmt(ready)}"))
        else:
            items.append(PlanItem(kind="order", icon="☕", tier="prediction",
                                  text=f"Order on arrival: about {wait_arrive[0]}–{wait_arrive[1]} min wait"))
        noise_icon = {"calm": "🔈", "moderate": "🔉", "lively": "🔊"}[ev.noise_label]
        items.append(PlanItem(kind="noise", icon=noise_icon, tier="prediction", text=f"Noise: {ev.noise_label}"))
        if needs_calls:
            if z.call_friendly:
                items.append(PlanItem(kind="calls", icon="📞", tier="policy", text="Calls are fine here, at low volume"))
            elif booth is not None:
                items.append(PlanItem(kind="calls", icon="📞", tier="recommendation",
                                      text=f"This is a no-calls zone. Take calls in the {booth.name}"))
                for ct in intent.call_times[:3]:
                    call_at = at_hm(ct, arrive)
                    if arrive <= call_at <= leave:
                        b = band(seat_likelihood(free_seats(cafe, booth, visits, now, call_at, exclude_id)))
                        items.append(PlanItem(kind="calls", icon="📞", tier="prediction",
                                              text=f"Your {ct} call: the {booth.name} is {b} to be free"))
        if need_power and z.has_power:
            items.append(PlanItem(kind="power", icon="🔌", tier="policy", text="Power sockets at this zone"))
        if laptop_peak and peak:
            items.append(PlanItem(kind="policy", icon="📋", tier="policy",
                                  text=f"During the {peak[0]}–{peak[1]} rush the café asks laptop stays to keep to {policy.laptop_limit_min_at_peak} min"))
        if steered:
            items.append(PlanItem(kind="policy", icon="📋", tier="policy", text="Staff are directing new guests to other zones right now"))
        leave_text = f"Leave by {fmt(leave)}"
        if deadline is not None:
            leave_text += f", {int(slack)} min before your {intent.leave_by} deadline" if slack >= 1 else f", right on your {intent.leave_by} deadline"
        items.append(PlanItem(kind="leave", icon="🏁", tier="recommendation", text=leave_text))

        tags = [ev.noise_label]
        if z.has_power:
            tags.append("plugs")
        tags.append("calls OK" if z.call_friendly else "no calls")
        if z.type == "outdoor":
            tags.append("open air")
        if z.type == "lounge":
            tags.append("sofas")

        option = PlanOption(
            id=f"{z.id}-{arrive.strftime('%H%M')}",
            zone_id=z.id, zone_name=z.name, zone_emoji=z.emoji,
            headline=" · ".join(tags),
            arrive_by=arrive, leave_by=leave, duration_min=duration,
            seat_likelihood=round(ev.likelihood, 2), seat_band=ev.band, free_at_arrival=round(ev.free, 1),
            wait_p50=wait_arrive[0], wait_p90=wait_arrive[1],
            noise_level=round(ev.noise, 1), noise_label=ev.noise_label,
            order_now=order_now, items=items, warnings=list(warnings), score=round(score, 2),
        )
        scored.append((score, option))

    scored.sort(key=lambda s: s[0], reverse=True)
    return [o for _, o in scored[:limit]]


def check_replan(cafe: Cafe, zones: list[Zone], visits: list[Visit], visit: Visit, now: datetime,
                 forced_reason: Optional[str] = None) -> Optional[tuple[str, list[PlanOption]]]:
    """Re-evaluate a confirmed visit against current conditions.

    Returns (reason, options) when the plan has meaningfully degraded. The last option, when present,
    is the refreshed current zone ("keep my plan").
    """
    if visit.status != "confirmed":
        return None
    arrive = max(visit.plan.arrive_by, ceil_minute(now))
    try:
        opts = make_options(cafe, zones, visits, visit.intent, now, arrive_override=arrive,
                            exclude_id=visit.id, keep_zone_id=visit.plan.zone_id, limit=6)
    except PlanError:
        return None
    current = next((o for o in opts if o.zone_id == visit.plan.zone_id), None)
    old = visit.plan
    reason = forced_reason
    if reason is None:
        if current is None:
            reason = f"{old.zone_name} no longer fits your plan"
        elif BAND_RANK[current.seat_band] < BAND_RANK[old.seat_band]:
            reason = f"{old.zone_name} is filling up faster than expected. A seat is now {current.seat_band}"
        elif current.wait_p50 >= old.wait_p50 + 3:
            reason = f"The counter got busier. The wait is now about {current.wait_p50}–{current.wait_p90} min"
        elif "quiet" in visit.intent.needs and old.noise_label == "calm" and current.noise_label != "calm":
            reason = f"{old.zone_name} is getting {current.noise_label}"
    if reason is None:
        return None
    others = [o for o in opts if o.zone_id != old.zone_id]
    # Only offer zones with better seat odds than staying put; fall back to the best others if none are.
    cur_rank = BAND_RANK[current.seat_band] if current else -1
    better = [o for o in others if BAND_RANK[o.seat_band] > cur_rank]
    alternatives = (better or others)[:2]
    options = alternatives + ([current] if current else [])
    return (reason, options) if options else None
