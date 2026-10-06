# Simula Native Ads API

A native ad server for AI-companion chat apps. It manages campaigns and ad sets, resolves users into
sessions, and serves sponsored-character ads into a feed. Ads are ranked by a LightGBM + factorization-machine
click model, and their copy is written by an LLM ahead of time through Temporal.

Built for the [Simula API take-home](docs/assignment.md): FastAPI · MongoDB · Redis · Temporal ·
Cloud Run.

- **Live:** _the deployed URL goes here_. It's deployed for the review window (landing page, `/demo`, `/docs`, `/ready`).
- **Sample output:** [samples/](samples/). The README's sample request and every GeoIP test IP, run against the production backends (MongoDB Atlas, Upstash Redis, Temporal Cloud, Gemini).

## Run it locally (about 5 minutes)

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) and
[uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs Python 3.13 by itself.

```bash
git clone https://github.com/rfoxes/Recommendation-Systems-Deployment.git
cd Recommendation-Systems-Deployment
cp .env.example .env            # works as-is; optionally add an LLM key (see below)
docker compose up -d --wait     # MongoDB (one-node replica set), Redis, Temporal dev server
uv sync                         # creates .venv with the pinned dependencies
uv run python -m app.seed       # loads data/*.json (insert-only, safe to re-run)
uv run uvicorn app.main:app     # first start downloads the CTR model (~3 MB) from its GitHub release
```

Then open:

| URL | What it is |
|---|---|
| http://localhost:8000 | Landing page with live status |
| http://localhost:8000/demo | The provided test harness serving **real ads**. Pick a README test IP and a device. |
| http://localhost:8000/docs | Every endpoint, with "Try it out" |
| http://localhost:8000/ready | Readiness of each dependency: a page in a browser, JSON for scripts |
| http://localhost:8233 | Temporal UI: the hourly cache-refresh schedule and copy-generation workflows |

**No keys are needed.** Without an LLM key, ads use each ad set's `fallback_copy`. To get LLM-written copy,
set `LLM_PROVIDER` and that provider's key in `.env` and restart. Gemini has a free key at
[aistudio.google.com](https://aistudio.google.com). Copy for every variant is generated in the background
within a few minutes; `/ready` shows progress.

## Try the API

The easiest way is the live demo page (`/demo`) and the API docs (`/docs`). From a terminal:

Country comes from the IP, and the README's test IPs can be sent in `X-Forwarded-For`. OS comes from the
`User-Agent`.

```bash
# A session for a user (ppid), or for the caller's IP if ppid is omitted
curl -s -X POST localhost:8000/session/create -H 'Content-Type: application/json' \
  -H 'X-Forwarded-For: 214.78.0.1' -d '{"ppid": "user_42"}'

# Serve an ad into feed slot 3 (US test IP, iPhone)
curl -s -X POST localhost:8000/load/native -H 'Content-Type: application/json' \
  -H 'X-Forwarded-For: 214.78.0.1' -A 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)' \
  -d '{"position": 3, "session_id": "<session_id>", "context": {"searchTerm": "space adventure",
       "tags": ["sci-fi", "rpg"], "category": "roleplay", "title": "Galaxy Companion", "nsfw": false}}'

# Campaigns: create (safe to retry with the same Idempotency-Key), list, update, delete
curl -s -X POST localhost:8000/campaigns -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
  -d '{"campaign_name": "Demo — US", "advertiser_company_id": "acmp_demo", "geo_targets": ["US"],
       "os_targets": ["ios"], "ios_store_url": "https://apps.apple.com/app/id1"}'
curl -s 'localhost:8000/campaigns?surface=native&active=true'
curl -s -X PATCH localhost:8000/campaigns/<campaign_id> -H 'Content-Type: application/json' -d '{"active": true}'

# Ad set: its variants are every combination of the asset lists
curl -s -X POST localhost:8000/adsets -H 'Content-Type: application/json' \
  -d '{"campaign_id": "<campaign_id>", "ad_set_name": "Heroes", "character_names": ["Luna", "Rex"],
       "video_urls": ["https://storage.googleapis.com/simula-public/assets/simula-campaigns/1781322499823-8.mp4"],
       "ctas": ["Play Free", "Install Now"], "ai_prompts": ["Excitedly tell a friend about the daily bonus."],
       "fallback_copy": ["Check this out!"]}'
```

| Test IP | Country |
|---|---|
| 214.78.0.1 | US |
| 2.125.160.217 | GB |
| 89.160.20.113 | SE |
| 175.16.199.1 | CN |
| 202.196.224.1 | PH |
| 67.43.156.1 | BT |

| Status | Meaning |
|---|---|
| 200 | An ad was served |
| 204 | No eligible campaign (geo, OS or store link) |
| 404 | Unknown session or campaign |
| 422 | Invalid request |
| 429 | Too many requests from one IP: 30 session creates or 120 ad requests a minute, with `Retry-After` |

## Tests

```bash
uv run pytest                                                 # 123 tests; needs `docker compose up`
uv run ruff check . && uv run ruff format --check . && uv run mypy   # lint, format, strict typing
uv run python scripts/smoke_test.py http://localhost:8000     # end to end; add --allow-fallback without an LLM key
uv run python scripts/sample_output.py http://localhost:8000  # writes samples/
```

- **`pytest`:** integration tests against real MongoDB, Redis and a Temporal test server. They never read
  your `.env`, so a real LLM key isn't used. Covered:
  - every README route
  - all six GeoIP test IPs, end to end
  - clicks and safe retries
  - transaction rollback and session expiry
  - the rate limit
  - cache consistency under concurrent writes
  - the ranker's exploration rules
  - template escaping
  - the LLM copy pipeline, with a fake LLM
  - the Temporal schedule and workflows
- **The smoke test:** 20 checks against a running deployment, from dependencies to clicks. It fails if ad
  copy came from the fallback.

## How it works

| README part | Implementation |
|---|---|
| Setup | MongoDB is the source of truth, run as a replica set so multi-document transactions work. Redis holds the campaign cache, sessions, a feature store and rate limits. Temporal runs the scheduled and background jobs. |
| Campaign routes | CRUD with a write-through Redis cache: versioned writes, so concurrent updates can't go backwards, and an atomic swap on refresh. An hourly Temporal schedule rebuilds the cache from MongoDB. DELETE cascades to ad sets and variants in one transaction. |
| Ad set routes | Variants are the cartesian product of the asset lists, de-duplicated and capped at 500. The ad set, its variants and the campaign link commit in one transaction. `Idempotency-Key` makes retries safe. |
| Sessions | The user comes from `ppid`, otherwise the IP. Get-or-create is one atomic Redis step. Sessions expire after 30 s without an ad serve, using Redis key expiry. Each IP is rate-limited. |
| Ad serving | Session, then GeoIP country and user-agent OS, then eligibility (geo, OS, store link), then features, the CTR model and the ranker, then a random ad set and variant, LLM copy and the rendered template. The serve record is written in a batch after the response. |
| CTR model + ranker | V1 (LightGBM + factorization machine) from [rfoxes/Recommendation-Systems](https://github.com/rfoxes/Recommendation-Systems), downloaded from a pinned release and checked against a golden sample at startup. Scoring uses numpy and LightGBM, no PyTorch. Its exploration-aware ranker is adapted to one request at a time. |
| LLM copy | Generated per variant by a Temporal workflow, on a rate-limited task queue. `LLM_PROVIDER` picks Gemini, Claude or OpenAI. A live call with a short timeout covers variants without lines yet, then the fallback copy. |
| Clicks | `POST /impressions/{id}/click`, called by the ad's CTA, authorized with a click-only key and counted once per impression. |

## Configuration

Everything comes from environment variables or `.env`; see [.env.example](.env.example). The main ones:

| Variable | Default | |
|---|---|---|
| `MONGO_URI`, `MONGO_DB` | local replica set, `simula` | |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY` | local dev server | Set all three for Temporal Cloud |
| `LLM_PROVIDER` | `gemini` | `gemini`, `anthropic` or `openai` |
| `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | unset | Only the chosen provider's key is needed |
| `LLM_MODEL` | provider default | `gemini-3.5-flash-lite`, `claude-opus-5-5`, `gpt-5.4-mini` |
| `API_URL`, `CLICK_API_KEY` | `http://localhost:8000`, `dev-click-key` | Embedded in each ad for click tracking |
| `SESSION_CREATE_LIMIT`, `SERVE_LIMIT_PER_MINUTE` | `30`, `120` | Per-IP limits per minute |

## Deploy (Google Cloud Run, free tier)

`scripts/deploy.sh` reads `.env.cloud` (template: [.env.cloud.example](.env.cloud.example)). It stores
secrets in Secret Manager and deploys one container (API plus Temporal worker) that scales to zero with at
most one instance. The image is built by Cloud Build from the [Dockerfile](Dockerfile).

## Layout

```
app/
  campaigns/  adsets/  sessions/   routes and storage for each README part
  serving/    ranking/  features/   /load/native, GeoIP, rendering, CTR model, ranker, feature store
  copywriting/  temporal/           LLM providers, workflows, schedule, worker
  demo/  web/  health.py            /demo harness, landing and readiness pages
data/  template/  prompts/          provided assets (seed data, ad template, LLM prompt, GeoIP database)
scripts/                            smoke test, sample output, deploy
tests/
```
