"""Shared data model. These shapes are stored in Cloud SQL (JSONB + key columns) and returned by the API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

HM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

Need = Literal["quiet", "power", "calls", "wifi", "meeting", "quick", "outdoor", "comfy"]
NEEDS: tuple[str, ...] = ("quiet", "power", "calls", "wifi", "meeting", "quick", "outdoor", "comfy")
Purpose = Literal["work", "meeting", "quick", "relax", "other"]
PURPOSES: tuple[str, ...] = ("work", "meeting", "quick", "relax", "other")
# How sure a plan line is. "policy" = a fact from café configuration, not a model output.
Tier = Literal["prediction", "recommendation", "commitment", "policy"]
ZoneType = Literal["quiet", "work", "social", "lounge", "outdoor", "booth"]
VisitStatus = Literal["proposed", "confirmed", "arrived", "seated", "done", "cancelled", "no_show"]
ACTIVE_STATUSES = ("confirmed", "arrived", "seated")


# ---------- café configuration ----------

class MenuItem(BaseModel):
    id: str
    name: str
    emoji: str = "☕"
    prep_sec: int = 120


class Policy(BaseModel):
    open_time: str = "07:00"
    close_time: str = "21:00"
    holds_enabled: bool = False
    hold_minutes: int = 10
    max_plannable_share: float = 0.6  # the rest of each zone stays for walk-ins
    peak_windows: list[list[str]] = Field(default_factory=lambda: [["12:00", "14:00"]])
    laptop_limit_min_at_peak: int = 90
    orders_paused: bool = False


class LiveState(BaseModel):
    baristas: int = 2
    queue_len: int = 3
    avg_prep_sec: int = 140
    overstay_min: float = 10.0  # learned: how long past their declared leave-by guests actually stay
    demand_multiplier: float = 1.0
    demand_until: Optional[datetime] = None


class Forecast(BaseModel):
    arrivals_per_hour: dict[str, float]  # hour "0".."23" -> expected arrivals
    seat_fraction: float = 0.55  # share of arrivals who sit down
    order_fraction: float = 0.95
    zone_share: dict[str, float]  # share of sit-down walk-ins per zone
    zone_dwell_min: dict[str, float]


class Zone(BaseModel):
    id: str
    name: str
    emoji: str
    type: ZoneType
    description: str = ""
    seats: int
    has_power: bool
    call_friendly: bool
    noise_baseline: float  # 1 (silent) .. 5 (loud)
    max_stay_min: Optional[int] = None
    position: int = 0  # display order on both apps
    occupied: int = 0
    pulse_noise: Optional[int] = None  # last staff tap, 1..5
    pulse_at: Optional[datetime] = None
    steer_until: Optional[datetime] = None  # staff accepted "steer new guests away"


class Cafe(BaseModel):
    id: str
    name: str
    neighbourhood: str
    timezone: str
    policy: Policy
    live: LiveState
    forecast: Forecast
    menu: list[MenuItem]


# ---------- customer intent ----------

class Intent(BaseModel):
    purpose: Purpose = "other"
    duration_min: Optional[int] = Field(default=None, ge=5, le=480)
    leave_by: Optional[str] = None  # "HH:MM", café time
    arrive_in_min: Optional[int] = Field(default=None, ge=0, le=180)
    party_size: int = Field(default=1, ge=1, le=8)
    needs: list[Need] = Field(default_factory=list, max_length=8)
    call_count: int = Field(default=0, ge=0, le=6)
    call_times: list[str] = Field(default_factory=list, max_length=6)
    order_item_ids: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("leave_by")
    @classmethod
    def _hm(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not HM_RE.match(v):
            raise ValueError("leave_by must be HH:MM")
        return v

    @field_validator("call_times")
    @classmethod
    def _hms(cls, v: list[str]) -> list[str]:
        for t in v:
            if not HM_RE.match(t):
                raise ValueError("call_times must be HH:MM")
        return v

    @field_validator("needs")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))


class Chip(BaseModel):
    label: str
    patch: dict


class Clarify(BaseModel):
    field: str
    text: str
    chips: list[Chip]


class IntentResult(BaseModel):
    intent: Intent
    source: Literal["gemini", "rules"]
    question: Optional[Clarify] = None
    summary: list[str] = Field(default_factory=list)  # human-readable "what we understood" chips


# ---------- plans ----------

class PlanItem(BaseModel):
    kind: str
    icon: str
    text: str
    tier: Tier


class PlanOption(BaseModel):
    id: str
    zone_id: str
    zone_name: str
    zone_emoji: str
    headline: str
    arrive_by: datetime
    leave_by: datetime
    duration_min: int
    seat_likelihood: float
    seat_band: Literal["likely", "possible", "unlikely"]
    free_at_arrival: float
    wait_p50: int
    wait_p90: int
    noise_level: float
    noise_label: Literal["calm", "moderate", "lively"]
    order_now: bool
    items: list[PlanItem]
    warnings: list[str] = Field(default_factory=list)
    score: float


class Order(BaseModel):
    item_ids: list[str]
    status: Literal["sent", "accepted", "ready", "collected"] = "sent"
    sent_at: datetime


class Replan(BaseModel):
    reason: str
    options: list[PlanOption]
    created_at: datetime


class Visit(BaseModel):
    id: str
    code: str
    status: VisitStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime  # rows past this are purged (24 h privacy retention)
    intent: Intent
    plan: PlanOption
    alternatives: list[PlanOption] = Field(default_factory=list)
    plan_version: int = 1
    order: Optional[Order] = None
    replan: Optional[Replan] = None
    text_redacted: Optional[str] = None
    arrived_at: Optional[datetime] = None
    seated_at: Optional[datetime] = None
    left_at: Optional[datetime] = None
    late_min: int = 0
    feedback: Optional[Literal["accurate", "off"]] = None


class Alert(BaseModel):
    id: str  # stable key, e.g. "shortfall:quiet"
    kind: str
    severity: Literal["info", "warn", "critical"]
    title: str
    text: str
    action: Optional[str] = None  # machine action applied on Accept
    action_label: Optional[str] = None
    status: Literal["open", "accepted", "dismissed"] = "open"
    created_at: datetime
    data: dict = Field(default_factory=dict)
