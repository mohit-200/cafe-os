"""Deterministic planner: capacity, waits, noise, zone choice and re-plan triggers."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import Intent, PlanOption, Visit
from app.planner import (PlanError, band, check_replan, free_seats, make_options, seat_likelihood, strip_state,
                         wait_minutes)


def _visit(vid: str, option: PlanOption, intent: Intent, status: str, now) -> Visit:
    return Visit(id=vid, code="X1", status=status, created_at=now, updated_at=now,
                 expires_at=now + timedelta(hours=24), intent=intent, plan=option)


def test_calls_route_away_from_quiet_zone(cafe, zones, now):
    intent = Intent(purpose="work", duration_min=90, needs=["quiet", "calls"], call_count=2)
    best = make_options(cafe, zones, [], intent, now)[0]
    assert best.zone_id == "window"  # call-friendly beats the no-calls quiet corner
    assert any(i.kind == "calls" and i.tier == "policy" for i in best.items)


def test_quiet_without_calls_gets_quiet_corner(cafe, zones, now):
    best = make_options(cafe, zones, [], Intent(purpose="work", duration_min=45, needs=["quiet"]), now)[0]
    assert best.zone_id == "quiet"
    assert best.noise_label == "calm"


def test_quiet_zone_with_calls_suggests_phone_booth(cafe, zones, now):
    opts = make_options(cafe, zones, [], Intent(purpose="work", duration_min=90, needs=["quiet", "calls"], call_count=1,
                                                call_times=["15:00"]), now)
    quiet = next(o for o in opts if o.zone_id == "quiet")
    texts = " ".join(i.text for i in quiet.items)
    assert "Phone Booth" in texts and "15:00 call" in texts


def test_groups_never_placed_in_quiet_zone(cafe, zones, now):
    opts = make_options(cafe, zones, [], Intent(purpose="meeting", duration_min=60, party_size=2), now, limit=6)
    assert opts and all(o.zone_id != "quiet" for o in opts)


def test_long_work_requires_power(cafe, zones, now):
    opts = make_options(cafe, zones, [], Intent(purpose="work", duration_min=120), now, limit=6)
    by_id = {z.id: z for z in zones}
    assert opts and all(by_id[o.zone_id].has_power for o in opts)


def test_large_party_only_fits_big_zones(cafe, zones, now):
    opts = make_options(cafe, zones, [], Intent(duration_min=60, party_size=8, purpose="meeting"), now, limit=6)
    assert {o.zone_id for o in opts} <= {"commons", "lounge", "patio"}


def test_party_larger_than_any_zone_gets_no_options(cafe, zones, now):
    zones_small = [z.model_copy(update={"seats": min(z.seats, 4)}) for z in zones]
    assert make_options(cafe, zones_small, [], Intent(duration_min=60, party_size=5), now) == []


def test_deadline_shortens_duration_with_warning(cafe, zones, now):
    best = make_options(cafe, zones, [], Intent(purpose="work", duration_min=120, leave_by="15:30"), now)[0]
    assert best.leave_by.strftime("%H:%M") <= "15:30"
    assert best.warnings and "15:30" in best.warnings[0]


def test_deadline_too_soon_is_rejected(cafe, zones, now):
    with pytest.raises(PlanError):
        make_options(cafe, zones, [], Intent(duration_min=30, leave_by="14:40"), now)


def test_closed_hours_rejected(cafe, zones, now):
    with pytest.raises(PlanError):
        make_options(cafe, zones, [], Intent(duration_min=30, arrive_in_min=180), now.replace(hour=20, minute=0))


def test_stay_capped_at_closing_time(cafe, zones, now):
    best = make_options(cafe, zones, [], Intent(duration_min=240), now.replace(hour=19, minute=0))[0]
    assert best.leave_by.strftime("%H:%M") == "21:00" and best.warnings


def test_quick_visit_defaults_to_ten_minutes_and_orders_ahead(cafe, zones, now):
    best = make_options(cafe, zones, [], Intent(purpose="quick", order_item_ids=["cold_brew"]), now)[0]
    assert best.duration_min == 10 and best.order_now


def test_declared_departure_frees_seat_after_overstay(cafe, zones, now):
    quiet = next(z for z in zones if z.id == "quiet")
    quiet.occupied = quiet.seats
    intent = Intent(purpose="work", duration_min=60, needs=["quiet"])
    seated_plan = make_options(cafe, zones, [], intent, now - timedelta(minutes=50))[0]
    seated_plan = seated_plan.model_copy(update={"zone_id": "quiet", "leave_by": now + timedelta(minutes=10)})
    seated = _visit("v1", seated_plan, intent, "seated", now)
    over = cafe.live.overstay_min
    before = free_seats(cafe, quiet, [seated], now, now + timedelta(minutes=10 + over - 1))
    after = free_seats(cafe, quiet, [seated], now, now + timedelta(minutes=10 + over + 1))
    assert after - before == pytest.approx(1.0, abs=0.15)


def test_confirmed_incoming_visit_consumes_capacity(cafe, zones, now):
    window = next(z for z in zones if z.id == "window")
    intent = Intent(purpose="work", duration_min=60)
    opt = make_options(cafe, zones, [], intent, now)[0].model_copy(update={"zone_id": "window"})
    t = opt.arrive_by + timedelta(minutes=5)
    alone = free_seats(cafe, window, [], now, t)
    with_visit = free_seats(cafe, window, [_visit("v1", opt, intent, "confirmed", now)], now, t)
    assert alone - with_visit == pytest.approx(1.0)


def test_plannable_share_reserves_walk_in_seats(cafe, zones, now):
    intent = Intent(purpose="work", duration_min=60, needs=["calls"], call_count=1)
    visits = []
    for i in range(3):  # 3 of 6 window seats planned; one more would exceed 60%
        opt = make_options(cafe, zones, visits, intent, now)[0]
        assert opt.zone_id == "window"
        visits.append(_visit(f"v{i}", opt, intent, "confirmed", now))
    nxt = make_options(cafe, zones, visits, intent, now, limit=6)
    assert all(o.zone_id != "window" for o in nxt)


def test_wait_grows_with_queue_and_fewer_baristas(cafe, now):
    base = wait_minutes(cafe, now, now)
    cafe.live.queue_len += 8
    busier = wait_minutes(cafe, now, now)
    cafe.live.baristas = 1
    understaffed = wait_minutes(cafe, now, now)
    assert base[0] < busier[0] < understaffed[0]
    assert all(p90 > p50 for p50, p90 in (base, busier, understaffed))


def test_staff_noise_pulse_changes_noise_label(cafe, zones, now):
    intent = Intent(purpose="work", duration_min=30, needs=["quiet"])
    calm = next(o for o in make_options(cafe, zones, [], intent, now, limit=6) if o.zone_id == "quiet")
    quiet = next(z for z in zones if z.id == "quiet")
    quiet.pulse_noise, quiet.pulse_at = 5, now
    loud = next(o for o in make_options(cafe, zones, [], intent, now, limit=6) if o.zone_id == "quiet")
    assert calm.noise_label == "calm" and loud.noise_label != "calm"


def test_likelihood_bands_and_strip_states():
    assert band(seat_likelihood(3)) == "likely"
    assert band(seat_likelihood(1)) == "possible"
    assert band(seat_likelihood(-1)) == "unlikely"
    assert strip_state(0.9, 1) == "ok"  # a 1-seat booth isn't "tight" when empty
    assert strip_state(0.9, 6) == "tight"
    assert strip_state(0.1, 6) == "full"


def test_every_plan_line_has_a_tier_and_no_unbacked_commitments(cafe, zones, now):
    for o in make_options(cafe, zones, [], Intent(purpose="work", duration_min=60, needs=["quiet"]), now, limit=6):
        assert all(i.tier in ("prediction", "recommendation", "commitment", "policy") for i in o.items)
        assert not any(i.tier == "commitment" for i in o.items)  # holds are disabled by café policy


def test_holds_enabled_adds_commitment_only_when_seat_likely(cafe, zones, now):
    cafe.policy.holds_enabled = True
    best = make_options(cafe, zones, [], Intent(purpose="work", duration_min=45, needs=["quiet"]), now)[0]
    assert best.seat_band == "likely" and any(i.tier == "commitment" for i in best.items)


def test_replan_when_zone_fills_offers_only_better_options(cafe, zones, now):
    intent = Intent(purpose="work", duration_min=60, needs=["quiet"])
    opt = make_options(cafe, zones, [], intent, now)[0]
    assert opt.zone_id == "quiet" and opt.seat_band == "likely"
    v = _visit("v1", opt, intent, "confirmed", now)
    next(z for z in zones if z.id == "quiet").occupied = 6
    reason, options = check_replan(cafe, zones, [v], v, now)
    assert "Quiet Corner" in reason
    current = next(o for o in options if o.zone_id == "quiet")
    alternatives = [o for o in options if o.zone_id != "quiet"]
    rank = {"unlikely": 0, "possible": 1, "likely": 2}
    assert alternatives and all(rank[o.seat_band] > rank[current.seat_band] for o in alternatives)


def test_no_replan_when_nothing_changed(cafe, zones, now):
    intent = Intent(purpose="work", duration_min=60, needs=["quiet"])
    v = _visit("v1", make_options(cafe, zones, [], intent, now)[0], intent, "confirmed", now)
    assert check_replan(cafe, zones, [v], v, now) is None


def test_steered_zone_is_deprioritised(cafe, zones, now):
    intent = Intent(purpose="work", duration_min=45, needs=["quiet"])
    assert make_options(cafe, zones, [], intent, now)[0].zone_id == "quiet"
    next(z for z in zones if z.id == "quiet").steer_until = now + timedelta(minutes=30)
    assert make_options(cafe, zones, [], intent, now)[0].zone_id != "quiet"
