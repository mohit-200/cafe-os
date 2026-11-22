# CAFÉ OS

**Know your visit before you walk in.** Customers say what they need from a café visit in one sentence and get an honest plan. The café sees what's coming, and when seats will free up, before it happens.

## Status
- [x] Phase 1: Design ([docs/DESIGN.md](docs/DESIGN.md), [DECISIONS.md](DECISIONS.md))
- [x] Phase 2: Infra bootstrap (`infra/setup.sh`), run 2026-11-22 against builder-pop-up-515511
- [x] Phase 3: Local build (this README)
- [x] Phase 4: 66 automated tests + 22-case Gemini eval (100%, all injection cases safe)
- [x] Phase 5: Deployed to Cloud Run: https://cafe-os-407891965358.asia-south1.run.app
- [ ] Phase 6: Operate (alerts, CI/CD, teardown)

## Run locally (no GCP needed)

```bash
cd cafe-os
./run_local.sh            # creates .venv on first run, serves http://127.0.0.1:8080
```

| Page | URL | Notes |
|---|---|---|
| Customer app | http://127.0.0.1:8080/ | Mobile-first. Use the browser's phone emulation for the demo. |
| Staff board | http://127.0.0.1:8080/staff | Dev PIN **2468** |
| API docs (dev only) | http://127.0.0.1:8080/api/docs | |

To test against a real PostgreSQL locally (same code path as Cloud SQL):
```bash
docker run -d --rm --name cafeos-pg -e POSTGRES_USER=cafeos -e POSTGRES_PASSWORD=localdev -e POSTGRES_DB=cafeos -p 55432:5432 postgres:17-alpine
STORAGE=postgres DB_PORT=55432 DB_PASSWORD=localdev ./run_local.sh
```

By default the app uses **in-memory storage** and the **rule-based intent parser**. The café clock starts at **14:30** so the demo behaves the same at any hour. Everything works without Gemini.

To try Gemini locally (optional):
```bash
export GEMINI_BACKEND=apikey GEMINI_API_KEY=<your AI Studio key>   # or: GEMINI_BACKEND=vertex GOOGLE_CLOUD_PROJECT=<id> after `gcloud auth application-default login`
./run_local.sh
```

### Environment variables
| Var | Default | Purpose |
|---|---|---|
| `APP_ENV` | `dev` | `prod` requires `SECRET_KEY` and `STAFF_PIN` and hides the API docs |
| `SECRET_KEY` | random in dev | Signs visit and staff tokens (Secret Manager in prod) |
| `STAFF_PIN` | `2468` in dev | Staff board PIN (Secret Manager in prod) |
| `STORAGE` | `memory` | `postgres` in prod (Cloud SQL) |
| `DB_HOST` / `DB_PORT` | `127.0.0.1` / `5432` | On Cloud Run: `/cloudsql/builder-pop-up-515511:asia-south1:cafe-os-db` |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `cafeos` / `cafeos` / — | Password from Secret Manager (`cafe-os-db-password`) in prod |
| `GOOGLE_CLOUD_PROJECT` | — | Used by the Vertex AI Gemini client |
| `GEMINI_BACKEND` | `off` | `vertex` (prod, uses the service account) or `apikey` (local) |
| `GEMINI_MODEL` / `GEMINI_LOCATION` | `gemini-3.1-flash-lite` / `global` | Chosen by eval (see evals/RESULTS.md) |
| `GEMINI_DAILY_CAP` | `300` | After this many calls the rule parser takes over (budget guard) |
| `DEMO_START` | `14:30` in dev | Pins café time. Unset it for real use. |
| `DEMO_CONTROLS` | `true` | Shows the scenario buttons on the staff board |

## Tests and eval

```bash
.venv/bin/pytest -q                                   # 63 tests: planner, intent, API, security (Postgres tests skip)
# Cloud SQL persistence tests against a local PostgreSQL 17:
docker run -d --rm --name cafeos-pg -e POSTGRES_USER=cafeos -e POSTGRES_PASSWORD=localdev -e POSTGRES_DB=cafeos -p 55432:5432 postgres:17-alpine
CAFEOS_TEST_PG=1 .venv/bin/pytest -q                  # all 66
# Gemini eval (22 cases, 5 prompt-injection attempts), results in evals/RESULTS.md:
GEMINI_BACKEND=vertex GOOGLE_CLOUD_PROJECT=builder-pop-up-515511 .venv/bin/python -m evals.run_eval
```

## Main user flows

1. **Plan:** On the customer app, type *"I need to work for 90 minutes, have two video calls, and get a flat white. I prefer a quiet corner, and I need to leave by 4 PM."* The plan picks **Window Bench**, not the Quiet Corner, because the Quiet Corner is a no-calls zone. It warns that 16:00 leaves only 84 minutes, and labels every line as a Prediction, Suggestion, Confirmed or Café rule.
2. **Confirm with one tap:** The flat white is pre-selected, so "Confirm visit & send order" sends it to the barista. Nothing is charged.
3. **Staff see it:** On the board, the guest's code appears under *Arriving* with needs icons, ETA, leave-by and the order. A "make this order first" alert appears because the plan is tight.
4. **Conditions change:** On the board, press **Lunch rush**. Zone strips turn amber and red, and ranked alerts appear with one-tap actions (*Steer to Community Table*, *Pause phone orders*).
5. **Re-plan:** Within about 3 seconds the customer's phone shows *"Heads up: things changed"*, with only better options plus *Keep my plan*.
6. **Visit lifecycle:** *I'm here* → *I'm seated* (the zone's seat count updates) → *Leaving now* (the seat frees immediately, and the café's learned overstay updates) → a one-tap accuracy rating.
7. **No sensors:** Staff use −/+ for seats and 🔈🔉🔊 for noise. These taps are the live data source.

## Architecture

**Stack: Cloud Run + Cloud SQL (PostgreSQL) + Gemini.** One Cloud Run service (FastAPI, static web) with an in-memory read model, written through to Cloud SQL. Gemini Flash-Lite on Vertex AI only extracts intent. The full diagram is in [docs/DESIGN.md](docs/DESIGN.md#g-google-cloud-architecture).

```
app/
  main.py         HTTP routes, validation, rate limits, security headers (CSP), JSON logs
  service.py      every state change → re-plan checks → alerts → write-through
  planner.py      deterministic engine: seat-time ledger, waits, noise, zone scoring, re-plan triggers
  intent.py       Gemini structured extraction + rule-based fallback, sanitising, redaction
  alerts.py       staff recommendations (rules + templates, no LLM)
  models.py       data model (Pydantic) shared by API and storage
  persistence.py  memory / Cloud SQL (PostgreSQL) backends, schema
  auth.py         HMAC visit tokens and staff tokens
  seed.py         sample café "Bean & Byte"
  clock.py        café-local clock with demo pinning
web/
  index.html, staff.html, static/{css,js,img}   no build step, no inline scripts
```

### API
| Method & path | Auth | Purpose |
|---|---|---|
| `GET /api/cafes/{cafe}/pulse?since=` | — | Live zone summary and wait |
| `POST /api/cafes/{cafe}/intent` | — | `{text}` → structured intent, optional clarifying question |
| `POST /api/cafes/{cafe}/plans` | — | `{intent}` → proposed visit + `token` |
| `GET /api/cafes/{cafe}/visits/{id}` | visit token | Visit, including any pending re-plan |
| `POST …/visits/{id}/confirm` | visit token | `{option_id, order_item_ids}`. One tap: confirms and sends the order. |
| `POST …/visits/{id}/action` | visit token | `arrived · seated · late · leaving · cancel · keep · feedback` |
| `POST …/visits/{id}/replan` | visit token | Accept a re-plan option |
| `POST /api/staff/login` | — | `{cafe_id, pin}` → staff token |
| `GET /api/staff/cafes/{cafe}/board?since=` | staff | Full board |
| `POST /api/staff/cafes/{cafe}/zones/{zone}` | staff | `{occupied_delta, noise}` |
| `POST /api/staff/cafes/{cafe}/live` | staff | `{baristas_delta, queue_delta}` |
| `POST /api/staff/cafes/{cafe}/alerts/{id}` | staff | `accept · dismiss` (accept applies the action) |
| `POST /api/staff/cafes/{cafe}/visits/{id}` | staff | `arrived · seated · left · no_show` |
| `POST /api/staff/cafes/{cafe}/orders/{id}` | staff | `accept · ready · collected` |
| `POST /api/staff/cafes/{cafe}/scenario` | staff | Demo: `lunch_rush · barista_break · quiet_fill · reset` |

## Demo script (under 3 minutes)
Use one laptop with two windows: the customer app in phone emulation on the left and the staff board on the right. Before starting, press **Reset demo** on the board. The full talk track is in [docs/DESIGN.md §L](docs/DESIGN.md#l-three-minute-demo-script-two-screens-side-by-side-phone-emulator-left-tablet-right).

## Deploy (Cloud Run + Cloud SQL + Gemini)

**Live:** customer app https://cafe-os-407891965358.asia-south1.run.app/ · staff board https://cafe-os-407891965358.asia-south1.run.app/staff (PIN in Secret Manager: `cafe-os-staff-pin`)

```bash
BILLING_ACCOUNT=<your-billing-id> ./infra/setup.sh   # once: APIs, service accounts, Cloud SQL, Artifact Registry, secrets, budget (idempotent)
./infra/deploy.sh    # every release: Cloud Build image -> Cloud Run revision -> live /health check
```

Read the staff PIN: `gcloud secrets versions access latest --secret=cafe-os-staff-pin --project=builder-pop-up-515511`

Cloud Run settings: `asia-south1`, runs as `cafe-os-run`, 0 to 1 instance, 1 vCPU / 512 MiB, public. Connects to Cloud SQL `builder-pop-up-515511:asia-south1:cafe-os-db` over the built-in connector socket. Secrets are injected from Secret Manager. Gemini is `gemini-3.1-flash-lite` on Vertex AI (global). `DEMO_START=14:30` pins the café clock for demos; deploy with `DEMO_START= ./infra/deploy.sh` for real time.

Health check path is `/health`. Cloud Run's front end reserves paths ending in `z`, so `/healthz` only works locally.
