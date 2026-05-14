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
- **LLM:** Anthropic Claude API (model: `claude-sonnet-4-6`). Use tool-use for structured outputs. Pricing (per MTok, verified via `claude-api` skill on 2026-05-13): input $3.00, output $15.00, 5-minute cache write $3.75 (1.25× input), cache read $0.30 (0.10× input). No prompt caching used in Phase 2 (per-restaurant HTML is unique per parse); revisit when stages reuse prompts.
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

## Pipeline Event Bus

In-memory pub/sub. Subscribers receive events for a single `restaurant_id`. Events are **not** persisted — they exist to drive the streaming UI and observability and are reconstructible from DB state if needed.

- Event names follow `"{stage}:{status}"`.
- `stage` ∈ `menu_parse | ingredient_enrich | usda_match | ams_fetch | distributor_discovery | rfp_send | quote_parse | recommend`.
- `status` ∈ `start | progress | complete | error`.
- Each `restaurant_id` keeps a **ring buffer of the last 10 events**; new subscribers receive the buffered events first, then live events.
- Service functions are decorated with `@stage("menu_parse")` which auto-emits `start` before and `complete` after, or `error` on raise.

## Idempotency

- **Menu re-parse**: deletes the restaurant's dishes (cascade clears `dish_ingredients`), then re-inserts dishes + dish_ingredients in a single short DB transaction. Ingredients are upserted on `normalized_name` (`INSERT … ON CONFLICT DO UPDATE … RETURNING id`) and are **not** deleted, since they're shared across restaurants and downstream RFPs.
- **LLM call boundary**: Claude is called *outside* the DB transaction (held by `traced_call` on its own session). The tool-use response shape is validated before opening the persistence transaction — failures raise before any DELETE.
- **`llm_usage` durability**: `traced_call` writes its row on a fresh session, so usage is logged even when the calling transaction rolls back.

## Pricing Data Source Decision

USDA's `api.nal.usda.gov/fdc/v1` (**FoodData Central**, FDC) is a nutrition and food-identification API — it does **not** publish retail or wholesale prices. Wholesale produce pricing lives in a separate USDA product: the **Agricultural Marketing Service (AMS) Market News** API at `marsapi.ams.usda.gov`, which aggregates daily price reports from terminal markets across the US.

Phase 3 uses both:

- **FDC** for identity / canonical food matching. Hits `POST /foods/search` (auth via `?api_key=`), excludes the `Branded` dataType (snack products pollute ingredient matches), and uses up to the top 5 hits. The top hit is auto-accepted if `score ≥ 200` OR `top / second ≥ 1.5`; otherwise Claude is asked via the `pick_fdc_match` tool. Sets `ingredients.usda_fdc_id` and `ingredients.category`.
- **AMS Market News** for prices. Auth is HTTP Basic with the API key as the username. The primary report is **Atlanta Terminal (`slug_id = 2278`)** — the closest USDA terminal market to Charlotte NC. Each ingredient maps via `commodity_map.py` to an AMS commodity slug (e.g. `kale → KALE`, `romaine → LETTUCE, ROMAINE`). Unmatched ingredients (proteins, oils, nuts, grains) receive one `pricing_unavailable=true` sentinel row — an honest gap, not a silent failure.

**Seed fallback.** USDA AMS occasionally times out, and the API key is gated on a free registration that the demo cannot assume. `data/seed_ams_prices.json` contains synthetic but plausible 30-day price series for all 15 mapped commodities. Live API failures (or a missing `USDA_AMS_API_KEY`) automatically fall back to seed; persisted rows use `source='ams_seed_fallback'` so they're honestly distinguishable from `source='ams_market_news'`.

**Commodity map seed (Phase 3, Atlanta Terminal):** kale, romaine, spinach, tomato, cucumber, onion (dry), avocado, broccoli, sweet potato, carrot, cabbage, bell pepper, cilantro, lemon, lime. The slug strings in `commodity_map.py` are placeholders verified by `scripts/verify_ams_map.py` against the live `/commodities` endpoint — re-run the script to keep them honest as USDA evolves the catalog.

## Trend Computation

Pure read-side compute over `ingredient_prices` rows (no persistence). For each ingredient:

- `latest_price` — most recent `price_per_unit` within the trend window.
- `avg_30d` — mean `price_per_unit` over the last 30 days.
- `delta_pct_30d` — `(latest - first_in_window) / first_in_window * 100`.
- `direction` — `up` if delta > +3%, `down` if delta < -3%, else `flat`. `unknown` when fewer than 2 in-window observations.

The ±3% threshold filters routine market noise; tune via `pricing_trends.DIRECTION_THRESHOLD_PCT`.

## Schema Delta — Migration 0002

Adds five columns to `ingredient_prices`:

| Column | Type | Purpose |
|---|---|---|
| `pricing_unavailable` | `BOOLEAN NOT NULL DEFAULT false` | Sentinel row for ingredients with no AMS match. |
| `ams_commodity_code` | `VARCHAR(120)` | AMS commodity slug. |
| `market_location` | `VARCHAR(120)` | E.g. `Atlanta Terminal`. |
| `price_per_unit` | `NUMERIC(12, 4)` | `$ / unit_normalized` for direct comparisons. |
| `unit_normalized` | `VARCHAR(40)` | Canonical unit (`lb`, `bunch`, `head`, `ea`). |

Plus a partial index `ix_ingredient_prices_observed` on `(ingredient_id, observed_at DESC) WHERE pricing_unavailable = false` to keep trend reads cheap as data grows.

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

## Distributor Discovery Strategy (Phase 4)

**Seed file is the primary data source.** `data/distributors_seed.json` contains 10 curated Charlotte/Gastonia NC area food wholesale distributors with realistic addresses, lat/long in actual industrial corridors, `*.example` email domains, and a mix of specialties chosen so the matching algorithm gets exercised (8 overlap with Sweetgreen's ingredient mix, 2 are deliberate non-overlaps — `Tidewater Seafood`, `Three Rivers Beverage Co.`).

**Google Places is optional enrichment**, gated on `GOOGLE_PLACES_API_KEY`. When set, two `places:searchNearby` calls (new API, `places.googleapis.com/v1/places:searchNearby` with `X-Goog-FieldMask`) are issued against the restaurant's lat/long with a 50km radius — one for `food_store`/`wholesaler` types, one for `grocery_store`/`supermarket` types. Results are deduplicated by `place_id` then by normalized name against existing seed rows.

**Places noise filter.** Google often returns retail chains (Harris Teeter, Costco/Sam's Club), individual restaurants, and non-food businesses caught by generic "wholesale" matching. A Claude tool-use pass (`classify_distributor_candidates` in `app/llm/tools.py`) accepts the batched candidate list and returns a `{is_wholesale_distributor, reason}` decision per candidate. The decision count is surfaced as `places_filtered_out` in the discovery result. Logged to `llm_usage` under stage `distributor_filter`.

**Merge policy when Places matches a seed entry.**
- **Seed wins authoritatively** on: name, address, latitude, longitude.
- **Places wins** on: phone, email, website (the point of optional enrichment is contact freshness).
- **Specialties** are union'd between seed tags and Places-derived tags.
- `source` becomes `'google_places_merged'` after a merge; brand-new Places records use `'google_places'`; pure seed rows keep `'seed'`.

**Matching algorithm** (`app/services/distributor_matching.py`, pure compute):
1. Translate each ingredient's FDC `category` plus name hints into a set of tags from a canonical specialty vocabulary (`produce`, `leafy_greens`, `tomatoes`, `protein_meat`, `protein_poultry`, `protein_seafood`, `dairy_eggs`, `dry_goods`, `oils`, `bakery`, `beverages`, `organic`, `specialty_ethnic`). `Spices and Herbs` → `produce + leafy_greens + dry_goods` so fresh herbs match produce distributors.
2. For each distributor, count restaurant ingredients with at least one tag overlap.
3. Sort by `matched_ingredient_count` desc, then Haversine distance from the restaurant asc.

**Seed roster:**

| Distributor | Specialty focus | City |
|---|---|---|
| Carolina Fresh Produce Co. | produce, leafy_greens, tomatoes, organic | Charlotte |
| Piedmont Wholesale Foods | produce, dry_goods, dairy_eggs, oils | Concord |
| Queen City Meats | protein_meat, protein_poultry | Charlotte |
| Southern Harvest Distributors | produce, dry_goods | Charlotte |
| Foothills Organic Distribution | produce, organic, leafy_greens | Gastonia |
| Catawba Valley Bakery Supply | bakery, dry_goods | Belmont |
| Carolina Dairy & Eggs | dairy_eggs | Mooresville |
| Charlotte Specialty Foods | specialty_ethnic, dry_goods, oils | Charlotte (NoDa) |
| Tidewater Seafood Distributors | protein_seafood (control — no Sweetgreen overlap) | Charlotte |
| Three Rivers Beverage Co. | beverages (control — no Sweetgreen overlap) | Pineville |

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

**Sweetgreen — Park Road Shopping Center, 4329 Park Rd, Charlotte, NC 28209.** Public HTML menu snapshot saved to `data/menus/sweetgreen.html`. Chosen because the menu is ingredient-forward (each dish lists its components), which exercises the parser's "high-confidence ingredient extraction" path well.

---

**Spec maintained by Claude Code — update as decisions evolve.**