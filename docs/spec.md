# Pathway RFP Automation — Project Spec

End-to-end pipeline that ingests a restaurant menu and produces distributor RFP quotes with a recommendation.

## Goals

1. Parse a restaurant menu into structured recipes + ingredients.
2. Enrich ingredients with USDA pricing data and trends.
3. Discover local distributors.
4. Send RFP emails to distributors.
5. Monitor inbox, parse quote replies, compare, recommend.

## Tech Stack

- **Backend:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic
- **DB:** PostgreSQL 16 (via Docker Compose)
- **LLM:** Anthropic Claude API (model: `claude-sonnet-4-5`). Use tool-use for structured outputs.
- **Email:** Resend API for send. IMAP polling (or Mailgun inbound routes) for receive.
- **Distributor discovery:** Google Places API; mock seed data as fallback.
- **USDA:** FoodData Central API (`api.nal.usda.gov/fdc/v1`).
- **Frontend:** Next.js 14 (App Router) + TypeScript + Tailwind + shadcn/ui. SSE for streaming.
- **Background jobs:** FastAPI `BackgroundTasks` for v1; upgrade to `arq` only if needed.
- **Config:** `pydantic-settings` reading from `.env`.
- **Tests:** `pytest` on the parser + USDA matcher + quote parser only (critical paths).

## High-Level Architecture

```
[Menu URL/PDF/Image]
        |
        v
+-------------------+      +------------------+
|  Menu Parser      |----->|  Recipes,        |
|  (Claude tool-use)|      |  Ingredients     |
+-------------------+      +------------------+
                                    |
                                    v
                           +------------------+
                           | USDA Pricing     |
                           | Worker           |
                           +------------------+
                                    |
                                    v
                           +------------------+
                           | Distributor      |
                           | Discovery        |
                           | (Google Places)  |
                           +------------------+
                                    |
                                    v
                           +------------------+
                           | RFP Email Sender |
                           | (Resend)         |
                           +------------------+
                                    |
                                    v
                           +------------------+
                           | Inbox Monitor +  |
                           | Quote Parser     |
                           | (Claude tool-use)|
                           +------------------+
                                    |
                                    v
                           +------------------+
                           | Recommendation   |
                           | Engine           |
                           +------------------+

UI (Next.js) <---- SSE stream + REST ---- FastAPI
```

Every stage reads/writes the DB. DB is the source of truth. Stages are independently triggerable for demo + recovery.

## Database Schema (initial)

```
restaurants
  id, name, address, city, state, zip, latitude, longitude, menu_source_url, created_at

dishes
  id, restaurant_id (fk), name, description, price, raw_text, parse_confidence (float), created_at

ingredients
  id, name, normalized_name, usda_fdc_id (nullable), category, created_at

dish_ingredients
  id, dish_id (fk), ingredient_id (fk), quantity, unit, estimation_confidence (float)

ingredient_prices
  id, ingredient_id (fk), usda_fdc_id, price, unit, source, observed_at, raw_payload (jsonb)

distributors
  id, name, address, phone, email, website, latitude, longitude,
  source (e.g. 'google_places', 'seed'), specialties (text[]), created_at

rfp_requests
  id, restaurant_id (fk), status, deadline, created_at

rfp_request_items
  id, rfp_request_id (fk), ingredient_id (fk), quantity, unit

rfp_emails
  id, rfp_request_id (fk), distributor_id (fk), direction ('out'|'in'),
  subject, body, message_id, in_reply_to, status, sent_at, received_at, raw_payload (jsonb)

quotes
  id, rfp_request_id (fk), distributor_id (fk), ingredient_id (fk),
  unit_price, unit, min_order_qty, delivery_days, terms, source_email_id (fk),
  parse_confidence (float), missing_fields (text[]), created_at

recommendations
  id, rfp_request_id (fk), distributor_id (fk), score, rationale, created_at

llm_usage
  id, stage, model, input_tokens, output_tokens, cost_usd, created_at
```

## Pipeline Stages — Detail

### Stage 1: Menu Parser
- **Input:** menu URL, uploaded PDF, or image path
- **Process:**
  - If URL → fetch + extract text (BeautifulSoup); fall back to screenshot + vision if JS-heavy
  - If PDF → extract text via `pypdf`; fall back to vision if image-only
  - If image → Claude vision directly
  - Call Claude with tool `extract_menu_items` returning `dishes[]` with `{name, description, price, estimated_ingredients: [{name, quantity, unit, confidence}]}`
- **Output:** rows in `dishes`, `ingredients`, `dish_ingredients`
- **Edge cases:** vague dish names ("Chef's Special"), prix fixe menus, allergen labels mistaken for ingredients. Flag with `parse_confidence < 0.7`.

### Stage 2: USDA Pricing
- **Input:** ingredient list from DB
- **Process:**
  - For each ingredient, call USDA `/foods/search` to find best match (use Claude to disambiguate if multiple hits and confidence < threshold)
  - Pull price history if available; otherwise pull current "Foundation" or "SR Legacy" data with nutrition + standard units
  - Compute 30-day delta if multi-point data exists
- **Output:** rows in `ingredient_prices`, `usda_fdc_id` filled on `ingredients`
- **Note:** USDA's free API doesn't expose retail pricing per se. Use the Agricultural Marketing Service (AMS) endpoints for commodity pricing, or fall back to nutritional/commodity data with a documented note in the README.

### Stage 3: Distributor Discovery
- **Input:** restaurant location, ingredient categories
- **Process:**
  - Google Places `Nearby Search` with type `food` + keyword `wholesale food distributor`
  - For each result, fetch Place Details for contact info
  - Dedupe; persist
  - **Fallback:** seed file `data/distributors_seed.json` with 5–8 mock distributors for the demo
- **Output:** rows in `distributors`

### Stage 4: RFP Email Sender
- **Input:** `rfp_request` with items + chosen distributors
- **Process:**
  - For each distributor: Claude composes a professional RFP email with ingredients, quantities, deadline, reply-to threading info
  - Send via Resend; capture `message_id`
  - Store in `rfp_emails` with `direction='out'`
- **Output:** sent emails persisted, status updated

### Stage 5: Inbox + Comparison (nice-to-have)
- **Input:** ongoing — inbox polled every N seconds (or webhook)
- **Process:**
  - IMAP poll for new messages, match `in_reply_to` to outbound `message_id`
  - Claude tool `parse_quote` extracts `{ingredient, unit_price, unit, min_order_qty, delivery_days, terms, missing_fields[]}`
  - If `missing_fields` non-empty → send a follow-up email asking for the missing items (one round only; cap follow-ups to avoid loops)
  - When all distributors have replied OR deadline passes → compute recommendation
- **Recommendation scoring:**
  - Weighted: 50% total cost, 20% delivery speed, 15% min order fit, 15% completeness/reliability
  - Persist score + rationale in `recommendations`

## Above-and-Beyond Features

1. **Confidence scores everywhere** — `parse_confidence`, `estimation_confidence`, `usda_match_confidence`. UI surfaces low-confidence items with yellow flags so the restaurant can review.
2. **Cost tracking** — every Claude call logs to `llm_usage`; UI dashboard tile shows total spend per pipeline run.
3. **Streaming pipeline UI** — backend emits SSE events on stage start/progress/complete; frontend animates stage cards live during a run.
4. **Lite follow-up agent** — for missing quote fields, agent sends one follow-up. Capped, idempotent.
5. *(Stretch)* Parser eval set — 5–10 hand-curated menu items with expected outputs; `make eval` runs them.

## API Surface (FastAPI)

```
POST /api/restaurants                    # create restaurant
POST /api/restaurants/{id}/menu          # ingest menu (URL/file)
POST /api/restaurants/{id}/pipeline/run  # kick off full pipeline
GET  /api/restaurants/{id}/pipeline/stream  # SSE events
GET  /api/restaurants/{id}/dishes
GET  /api/ingredients/{id}/prices
GET  /api/restaurants/{id}/distributors
POST /api/restaurants/{id}/rfp           # create RFP + send emails
GET  /api/rfp/{id}/quotes
GET  /api/rfp/{id}/recommendation
GET  /api/usage                          # llm_usage rollup
```

## Phase Plan (each gets its own `/plan` in Claude Code)

| # | Phase | Est. hrs |
|---|---|---|
| 1 | Foundation (repo, Docker, FastAPI skeleton, models, migrations, config, logging) | 2 |
| 2 | Stage 1: Menu Parser + ingestion + Claude tool-use | 3 |
| 3 | Stage 2: USDA pricing + matching | 2 |
| 4 | Stage 3: Distributor discovery + seed fallback | 2 |
| 5 | Stage 4: RFP email composition + send via Resend | 2 |
| 6 | Stage 5: Inbox monitor + quote parser + lite follow-up + recommendation | 4 |
| 7 | Next.js UI: pipeline view, streaming events, comparison, cost dashboard | 3 |
| 8 | Polish: README, seed data, demo script, Loom recording | 2 |

**Buffer:** ~4h. Eval set if time allows.

## Repository Layout

```
pathway-rfp/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── routers/
│   │   ├── services/        # menu_parser, usda, distributors, email, quote_parser, recommender
│   │   ├── llm/             # claude client + tool defs + usage tracking
│   │   ├── pipeline/        # orchestrator + SSE event bus
│   │   └── utils/
│   ├── alembic/
│   ├── tests/
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/
│   ├── app/                 # Next.js App Router
│   ├── components/
│   ├── lib/
│   ├── package.json
│   └── Dockerfile
├── data/
│   ├── menus/               # the pinned restaurant menu we use
│   └── distributors_seed.json
├── docs/
│   └── spec.md              # this file
├── docker-compose.yml
├── .env.example
├── Makefile
├── README.md
└── .gitignore
```

## Environment Variables (`.env.example`)

```
DATABASE_URL=postgresql+asyncpg://pathway:pathway@db:5432/pathway
ANTHROPIC_API_KEY=
USDA_API_KEY=
GOOGLE_PLACES_API_KEY=
RESEND_API_KEY=
RESEND_FROM_EMAIL=rfp@yourdomain.com
IMAP_HOST=
IMAP_USER=
IMAP_PASSWORD=
LOG_LEVEL=INFO
```

## Non-Goals (explicit cuts)

- User authentication / multi-tenancy
- Production deployment
- Comprehensive test coverage (only critical paths)
- Mobile UI
- Internationalization
- Full negotiation agent (cap at one follow-up)

## Restaurant Choice

To be filled in Phase 1. Pick a real local restaurant with a clear, public menu (PDF or HTML). Save a copy to `data/menus/` for reproducibility.

---

**Spec maintained by Claude Code — update as decisions evolve.**