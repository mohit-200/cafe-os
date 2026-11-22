"""Realistic sample café used for local runs, tests and the live demo."""

from __future__ import annotations

from .models import Cafe, Forecast, LiveState, MenuItem, Policy, Zone


def sample_cafe(cafe_id: str = "bean-byte") -> Cafe:
    return Cafe(
        id=cafe_id,
        name="Bean & Byte",
        neighbourhood="Indiranagar, Bengaluru",
        timezone="Asia/Kolkata",
        policy=Policy(),
        live=LiveState(baristas=2, queue_len=2, avg_prep_sec=140, overstay_min=10.0),
        forecast=Forecast(
            # Typical weekday walk-ins per hour (sample data, would come from POS history).
            arrivals_per_hour={
                "7": 12, "8": 28, "9": 32, "10": 24, "11": 22, "12": 40, "13": 42,
                "14": 24, "15": 22, "16": 26, "17": 24, "18": 16, "19": 12, "20": 8,
            },
            seat_fraction=0.55,
            zone_share={"quiet": 0.17, "window": 0.18, "commons": 0.25, "lounge": 0.2, "patio": 0.15, "booth": 0.05},
            zone_dwell_min={"quiet": 75, "window": 70, "commons": 50, "lounge": 55, "patio": 40, "booth": 15},
        ),
        menu=[
            MenuItem(id="flat_white", name="Flat white", emoji="☕", prep_sec=150),
            MenuItem(id="cappuccino", name="Cappuccino", emoji="☕", prep_sec=150),
            MenuItem(id="latte", name="Latte", emoji="🥛", prep_sec=150),
            MenuItem(id="espresso", name="Espresso", emoji="☕", prep_sec=80),
            MenuItem(id="cold_brew", name="Cold brew", emoji="🧊", prep_sec=60),
            MenuItem(id="masala_chai", name="Masala chai", emoji="🫖", prep_sec=120),
            MenuItem(id="croissant", name="Butter croissant", emoji="🥐", prep_sec=60),
            MenuItem(id="avocado_toast", name="Avocado toast", emoji="🥑", prep_sec=300),
        ],
    )


def sample_zones() -> list[Zone]:
    zones = [
        Zone(id="quiet", name="Quiet Corner", emoji="🤫", type="quiet",
             description="Library-quiet, no calls, plugs at every seat",
             seats=6, has_power=True, call_friendly=False, noise_baseline=1.4, occupied=3),
        Zone(id="window", name="Window Bench", emoji="🪟", type="work",
             description="Bright counter seating, plugs, calls OK at low volume",
             seats=6, has_power=True, call_friendly=True, noise_baseline=2.3, occupied=3),
        Zone(id="commons", name="Community Table", emoji="🪵", type="social",
             description="Big shared table, buzzy, a few plugs",
             seats=10, has_power=True, call_friendly=False, noise_baseline=3.0, occupied=6),
        Zone(id="lounge", name="Sofa Lounge", emoji="🛋️", type="lounge",
             description="Sofas for meetings and catch-ups",
             seats=8, has_power=False, call_friendly=True, noise_baseline=3.0, occupied=4),
        Zone(id="patio", name="Garden Patio", emoji="🌿", type="outdoor",
             description="Open air, calls welcome",
             seats=8, has_power=False, call_friendly=True, noise_baseline=2.7, occupied=2),
        Zone(id="booth", name="Phone Booth", emoji="📞", type="booth",
             description="Soundproof 1-person booth for calls, 30 min max",
             seats=1, has_power=True, call_friendly=True, noise_baseline=1.0, max_stay_min=30, occupied=0),
    ]
    for i, z in enumerate(zones):
        z.position = i
    return zones
