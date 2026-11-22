"""HTTP API and static web app for CAFÉ OS."""

from __future__ import annotations

import hmac
import json
import logging
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import check_staff_token, check_visit_token, staff_token, visit_token
from .config import load_settings
from .intent import IntentExtractor
from .models import Intent
from .persistence import make_persistence
from .planner import PlanError
from .service import CafeService, Conflict, NotFound

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class JsonFormatter(logging.Formatter):
    """One JSON object per line, which Cloud Logging parses into structured entries."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name}
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
log = logging.getLogger("cafe_os")

settings = load_settings()
service = CafeService(settings, make_persistence(settings), IntentExtractor(settings))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await service.startup()
    yield


app = FastAPI(title="CAFÉ OS", version="0.1.0", lifespan=lifespan, docs_url="/api/docs" if settings.app_env == "dev" else None,
              redoc_url=None, openapi_url="/api/openapi.json" if settings.app_env == "dev" else None)


# ---------- cross-cutting ----------

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

_hits: dict[str, deque] = defaultdict(deque)
LIMITS = {"intent": (20, 60), "plans": (20, 60), "login": (6, 60), "api": (300, 60)}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def rate_limit(bucket: str, request: Request) -> None:
    limit, window = LIMITS[bucket]
    key = f"{bucket}:{client_ip(request)}"
    now = time.monotonic()
    hits = _hits[key]
    while hits and hits[0] < now - window:
        hits.popleft()
    if len(hits) >= limit:
        raise HTTPException(429, "Too many requests, please slow down")
    hits.append(now)


@app.middleware("http")
async def headers_and_limits(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        try:
            rate_limit("api", request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(NotFound)
async def _not_found(_: Request, exc: NotFound):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(Conflict)
async def _conflict(_: Request, exc: Conflict):
    return JSONResponse({"detail": str(exc), "options": [o.model_dump(mode="json") for o in exc.options]}, status_code=409)


@app.exception_handler(PlanError)
async def _plan_error(_: Request, exc: PlanError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(PermissionError)
async def _forbidden(_: Request, exc: PermissionError):
    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.exception_handler(ValueError)
async def _bad_request(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def require_visit(cafe_id: str, visit_id: str, x_visit_token: Optional[str] = Header(None)) -> None:
    if not check_visit_token(settings.secret_key, cafe_id, visit_id, x_visit_token):
        raise HTTPException(403, "Not your visit")


def require_staff(cafe_id: str, authorization: Optional[str] = Header(None)) -> None:
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    if not check_staff_token(settings.secret_key, cafe_id, token):
        raise HTTPException(401, "Staff sign-in required")


# ---------- request bodies ----------

class IntentBody(BaseModel):
    text: str = Field(min_length=2, max_length=500)


class PlanBody(BaseModel):
    intent: Intent
    text: Optional[str] = Field(None, max_length=500)


class ConfirmBody(BaseModel):
    option_id: str = Field(max_length=64)
    order_item_ids: list[str] = Field(default_factory=list, max_length=4)


class VisitActionBody(BaseModel):
    action: Literal["arrived", "seated", "late", "leaving", "cancel", "keep", "feedback"]
    minutes: int = Field(0, ge=0, le=60)
    feedback: Optional[Literal["accurate", "off"]] = None


class ReplanBody(BaseModel):
    option_id: str = Field(max_length=64)


class LoginBody(BaseModel):
    cafe_id: str = Field(max_length=64)
    pin: str = Field(min_length=4, max_length=12)


class ZoneBody(BaseModel):
    occupied_delta: int = Field(0, ge=-4, le=4)
    noise: Optional[int] = Field(None, ge=1, le=5)


class LiveBody(BaseModel):
    baristas_delta: int = Field(0, ge=-1, le=1)
    queue_delta: int = Field(0, ge=-5, le=5)


class AlertBody(BaseModel):
    action: Literal["accept", "dismiss"]


class StaffVisitBody(BaseModel):
    action: Literal["arrived", "seated", "left", "no_show"]


class OrderBody(BaseModel):
    action: Literal["accept", "ready", "collected"]


class ScenarioBody(BaseModel):
    name: Literal["lunch_rush", "barista_break", "quiet_fill", "reset"]


# ---------- pages ----------

def _page(name: str) -> FileResponse:
    return FileResponse(WEB_DIR / name, headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
async def customer_home():
    return _page("index.html")


@app.get("/c/{cafe_id}", include_in_schema=False)
async def customer_cafe(cafe_id: str):
    return _page("index.html")


@app.get("/staff", include_in_schema=False)
async def staff_page():
    return _page("staff.html")


@app.get("/health", include_in_schema=False)
@app.get("/healthz", include_in_schema=False)  # local only: Cloud Run's front end reserves paths ending in "z"
async def health():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


# ---------- customer API ----------

@app.get("/api/config")
async def config():
    return {"cafe_id": settings.cafe_id}


@app.get("/api/cafes/{cafe_id}/pulse")
async def pulse(cafe_id: str, since: Optional[str] = None):
    await service.tick(cafe_id)
    if since and since == service.version(cafe_id):
        return {"unchanged": True, "version": since}
    return service.pulse_view(cafe_id)


@app.post("/api/cafes/{cafe_id}/intent")
async def intent(cafe_id: str, body: IntentBody, request: Request):
    rate_limit("intent", request)
    result = await service.parse_intent(cafe_id, body.text)
    return result.model_dump(mode="json")


@app.post("/api/cafes/{cafe_id}/plans")
async def create_plan(cafe_id: str, body: PlanBody, request: Request):
    rate_limit("plans", request)
    visit = await service.create_plan(cafe_id, body.intent, body.text)
    return {"visit": service.visit_view(cafe_id, visit.id),
            "token": visit_token(settings.secret_key, cafe_id, visit.id)}


@app.get("/api/cafes/{cafe_id}/visits/{visit_id}", dependencies=[Depends(require_visit)])
async def get_visit(cafe_id: str, visit_id: str):
    await service.tick(cafe_id)
    return service.visit_view(cafe_id, visit_id)


@app.post("/api/cafes/{cafe_id}/visits/{visit_id}/confirm", dependencies=[Depends(require_visit)])
async def confirm_visit(cafe_id: str, visit_id: str, body: ConfirmBody):
    await service.confirm(cafe_id, visit_id, body.option_id, body.order_item_ids)
    return service.visit_view(cafe_id, visit_id)


@app.post("/api/cafes/{cafe_id}/visits/{visit_id}/action", dependencies=[Depends(require_visit)])
async def visit_action(cafe_id: str, visit_id: str, body: VisitActionBody):
    await service.customer_action(cafe_id, visit_id, body.action, body.minutes, body.feedback)
    return service.visit_view(cafe_id, visit_id)


@app.post("/api/cafes/{cafe_id}/visits/{visit_id}/replan", dependencies=[Depends(require_visit)])
async def accept_replan(cafe_id: str, visit_id: str, body: ReplanBody):
    await service.accept_replan(cafe_id, visit_id, body.option_id)
    return service.visit_view(cafe_id, visit_id)


# ---------- staff API ----------

@app.post("/api/staff/login")
async def staff_login(body: LoginBody, request: Request):
    rate_limit("login", request)
    service.state(body.cafe_id)
    if not hmac.compare_digest(body.pin, settings.staff_pin):
        raise HTTPException(401, "Wrong PIN")
    return {"token": staff_token(settings.secret_key, body.cafe_id)}


@app.get("/api/staff/cafes/{cafe_id}/board", dependencies=[Depends(require_staff)])
async def board(cafe_id: str, since: Optional[str] = None):
    await service.tick(cafe_id)
    if since and since == service.version(cafe_id):
        return {"unchanged": True, "version": since}
    return service.board_view(cafe_id)


@app.post("/api/staff/cafes/{cafe_id}/zones/{zone_id}", dependencies=[Depends(require_staff)])
async def zone_update(cafe_id: str, zone_id: str, body: ZoneBody):
    await service.zone_update(cafe_id, zone_id, body.occupied_delta, body.noise)
    return service.board_view(cafe_id)


@app.post("/api/staff/cafes/{cafe_id}/live", dependencies=[Depends(require_staff)])
async def live_update(cafe_id: str, body: LiveBody):
    await service.live_update(cafe_id, body.baristas_delta, body.queue_delta)
    return service.board_view(cafe_id)


@app.post("/api/staff/cafes/{cafe_id}/alerts/{alert_id}", dependencies=[Depends(require_staff)])
async def alert_action(cafe_id: str, alert_id: str, body: AlertBody):
    await service.alert_action(cafe_id, alert_id, body.action)
    return service.board_view(cafe_id)


@app.post("/api/staff/cafes/{cafe_id}/visits/{visit_id}", dependencies=[Depends(require_staff)])
async def staff_visit(cafe_id: str, visit_id: str, body: StaffVisitBody):
    await service.staff_visit_action(cafe_id, visit_id, body.action)
    return service.board_view(cafe_id)


@app.post("/api/staff/cafes/{cafe_id}/orders/{visit_id}", dependencies=[Depends(require_staff)])
async def order_action(cafe_id: str, visit_id: str, body: OrderBody):
    await service.order_action(cafe_id, visit_id, body.action)
    return service.board_view(cafe_id)


@app.post("/api/staff/cafes/{cafe_id}/scenario", dependencies=[Depends(require_staff)])
async def scenario(cafe_id: str, body: ScenarioBody):
    await service.scenario(cafe_id, body.name)
    return service.board_view(cafe_id)
