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
POST /api/restaurants/{id}/send_rfps     # Phase 5 — compose + send RFP emails
GET  /api/rfp/{id}                       # Phase 5 — RFP audit: request + items + emails
GET  /api/restaurants/{id}/rfps          # Phase 5 — list RFPs for a restaurant
GET  /api/rfp/{id}/quotes                # Phase 6 — distributor quote rollup
GET  /api/rfp/{id}/recommendation        # Phase 6 — final distributor recommendation
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
USDA_FDC_API_KEY=
USDA_AMS_API_KEY=
GOOGLE_PLACES_API_KEY=
RESEND_API_KEY=
RESEND_FROM_EMAIL=                                 # legacy alias — Phase 5 uses RFP_FROM_EMAIL
RFP_FROM_EMAIL=procurement@getserviceledger.com    # Phase 5 send identity
RFP_DEMO_INBOX=daniel@getserviceledger.com         # Phase 5 demo recipient override target
COVERS_PER_DISH_PER_WEEK=150                       # Phase 5 quantity-aggregation planning estimate
IMAP_HOST=                                         # Phase 6 inbox monitor (Workspace app password required)
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

## RFP Email Strategy (Phase 5)

**End-to-end:** `POST /api/restaurants/{id}/send_rfps` (or `cli send-rfps`) scores distributors with the Phase 4 matcher, picks the top N where `matched_ingredient_count >= min_matches`, aggregates per-ingredient weekly volumes across the menu, composes one Claude-written email per distributor scoped to *that distributor's* matched ingredients, and sends via Resend. Each send is persisted to `rfp_emails` with our minted RFC-822 Message-ID, the demo recipient override, and Resend's response id. One failed send (network, 4xx, 5xx) is logged as `status='failed'` and never aborts the batch — the parent `rfp_request.status` becomes `partial` if any sends failed and others succeeded, `sent` if all succeeded.

**Defaults:** `distributor_limit=5`, `min_matches=2`, `deadline_days=5`. The `min_matches>=2` floor excludes single-match outliers (e.g. the Tidewater Seafood control ends up with one match because "chicken" hint-tags through the seafood specialty), so Phase 5 demos only meaningful matches.

**From-address:** `procurement@getserviceledger.com` (env `RFP_FROM_EMAIL`). The domain is Resend-verified, so any local-part on `getserviceledger.com` works. `Reply-To` mirrors the From so distributor replies route to the inbox Phase 6 monitors.

### Quantity aggregation + wording-variant dedupe

Per-serving quantities × `COVERS_PER_DISH_PER_WEEK` (default 150) → weekly volume per ingredient. Body text explicitly labels these as planning estimates ("distributors should quote at their standard wholesale tiers, NOT treat this as a firm purchase order") to set expectations.

**Wording-variant dedupe** prevents asking one distributor for the same physical ingredient twice. The aggregator computes a canonical root by stripping qualifier adjectives (`organic`, `fresh`, `shredded`, `chopped`, `vine ripe`, `antibiotic-free`, etc.) and conservative plural endings. Rows sharing a root collapse under one display name; their weekly quantities sum *only when units agree* (mixed `oz` + `lb` → drop the quantity rather than mix). Color words (`red onion` vs `green onion`) and cheese types (`goat` vs `cheddar`) are deliberately preserved.

### Demo recipient override

Distributors keep their `.example` placeholder emails in the DB as honest placeholders. At send time the orchestrator generates a slug from the distributor name (`Carolina Fresh Produce Co.` → `carolina-fresh-produce-co`) and rewrites the To address to `daniel+{slug}@getserviceledger.com`. Both addresses are stored on the `rfp_emails` row (`recipient_actual`, `recipient_nominal`) so the audit trail never lies about what we *would* have sent vs what we *actually* sent.

The plus-addressed mailbox is a Google Workspace account; all `daniel+anything@getserviceledger.com` mail lands in `daniel@getserviceledger.com`'s INBOX, which Phase 6's IMAP monitor will poll.

### Threading (sets up Phase 6 reply matching)

We mint our own RFC-822 Message-ID and pass it via Resend's `headers` parameter. Resend's response `id` is captured separately (`resend_id` column) — it is Resend's internal handle, not the on-the-wire Message-ID.

Format: `<rfp-{rfp_request_id}-{distributor_id}-{8-hex}@getserviceledger.com>`.

Phase 6 will match inbound replies in three layers (most specific first):
1. **`In-Reply-To` header** → exact match on `rfp_emails.message_id`. Primary signal.
2. **Plus-tag in To address** → derive distributor slug, find latest open rfp_emails for that distributor. Survives clients that strip threading headers.
3. **Subject prefix `[RFP-{id}]`** → match by rfp_request_id. Last-resort fallback when both above fail.

### Schema delta — migration `0003_rfp_recipient`

`rfp_emails` adds three columns (all nullable, additive):

| Column | Type | Purpose |
|---|---|---|
| `recipient_actual` | `VARCHAR(320)` | Address Resend actually sent to (`daniel+slug@…`) |
| `recipient_nominal` | `VARCHAR(320)` | Distributor's stored email (`orders@*.example`) |
| `resend_id` | `VARCHAR(120)`, indexed | Resend's response `id` for support/lookup |

### API surface added in Phase 5

```
POST /api/restaurants/{id}/send_rfps         # body: {distributor_limit?, min_matches?, deadline_days?}
GET  /api/rfp/{rfp_request_id}               # full audit: request + items + emails
GET  /api/restaurants/{id}/rfps              # list of RFPs (summary)
```

CLI:

```
python -m app.cli send-rfps <restaurant_id> [--limit N] [--min-matches M] [--deadline-days D]
```

LLM usage: `stage='rfp_compose'`, one call per distributor.

### Phase 5.1 — Quality patches (2026-05-14)

The first live send surfaced three real issues; the patch addresses each.

**Issue 1 — Broken opening sentence.** The composer tool description allowed Claude to leave a name placeholder blank ("My name is with the procurement team at..."). The tool description and the per-call user message now hard-mandate the exact opening pattern: `"I'm reaching out from the procurement team at {restaurant name} in {city, state}."` — no personal-name placeholder, ever. Closing signature is fixed to `Procurement Team / {restaurant}`.

**Issue 2 — Matching leakage.** Two distinct bugs in `distributor_matching.specialty_tags_for`:

1. **Substring-in-substring false positive.** The `tea` hint matched on "s**tea**k", routing `caramelized garlic steak` to Three Rivers Beverage. Fix: name hints now use `\b<word>\b` word-boundary regexes.
2. **Composite-name leak.** The `cilantro` hint fired inside `Lime Cilantro Jalapeño Sauce`, routing a prepared sauce to Carolina Fresh Produce. Sweetgreen makes sauces/dressings in-house — routing them to a raw-produce distributor invents a supply relationship. Fix: a composite-name guard (`\b(sauce|dressing|vinaigrette|marinade|glaze|aioli|syrup|spread|pesto|salsa|hummus|tahini|paste|mayo|mayonnaise|compound butter)\b`) returns an empty tag set before any other rule fires.

The orchestrator now **re-applies `min_matches` AFTER the tightened scoping**, so a distributor whose match count collapses below the floor (e.g., if all its v1 matches were leak-driven) is dropped from the batch entirely. Items that no selected distributor can cover are surfaced in `RfpSendResult.unassigned_ingredients` rather than silently routed to a weak match — honest gap surfacing that the demo writeup can point to.

**Issue 3 — Per-serving units → wholesale units.** v1 emails asked for `~600 tbsp/week of basil` and `~4,800 fl oz/week of kombucha` — not how wholesale prices. `quantity_aggregator.normalize_to_wholesale_unit` now converts per-serving units to plausible wholesale units after collapse:

| From unit | Context (name match) | To | Factor | Note |
|---|---|---|---|---|
| `tbsp` | herbs: basil/cilantro/parsley/mint/oregano/thyme/rosemary/dill/sage/chive | `bunch` | qty / 50 | "≈ N bunches at planning density; please confirm your standard bunch size" |
| `cup` | same herb list | `bunch` | qty / 3 | same caveat |
| `cup` | leafy greens: kale/romaine/spinach/arugula/lettuce/spring mix | `lb` | qty × 0.0625 | ~1 oz/cup chopped |
| `cup` | tomato(es) | `lb` | qty × 0.40 | ~6.5 oz/cup chopped |
| `cup` | grains: rice/quinoa/farro/oats/barley/couscous | `lb` | qty × 0.40 | dry ~6.4 oz/cup |
| `cup` | legumes: chickpea/black bean/lentil/white bean/pinto | `lb` | qty × 0.45 | dry ~7.2 oz/cup |
| `cup` | liquid names: kombucha/lemonade/juice/tea/broth/stock/milk/cream/oil | `gallon` | qty / 16 | 16 cups/gal |
| `fl oz` | any | `gallon` | qty / 128 | 128 fl oz/gal |
| `oz` (weight) | any | `lb` | qty / 16 | 16 oz/lb |
| `slice` / `piece` | bread/bagel/pita/tortilla/naan | `dozen` | qty / 12 | bakery norm |
| `ea`, `bunch`, `head`, `case`, `lb`, `gallon`, `dozen` | — | passthrough | 1 | already wholesale |
| anything else | — | original | 1 | `conversion_note="no wholesale rule applies; please quote in your standard unit"` |

The raw per-serving aggregate is still stored on `rfp_request_items.quantity/unit` for audit. The email body shows `wholesale_quantity` + `wholesale_unit` with the `conversion_note` inline.

**Sanity ceilings.** Post-conversion outputs are sanity-checked. If a converted value exceeds `10,000 lb` or `1,000 gal` per week, or rounds below `0.01` of the unit, `conversion_note` is appended with a flag for review — a cheap guard against a conversion-factor typo silently shipping to the distributor.

### Phase 5 verified run (2026-05-14)

Sweetgreen Park Rd (restaurant_id=1), 102 ingredients across 45 dishes, 10 distributors loaded (Places noise filter rejected 24 retail chains, 0 kept).

Send result with defaults (`min_matches=2`, `limit=5`, `deadline_days=5`):

| Distributor | Matched ingredients | Status | Recipient (actual) |
|---|---:|---|---|
| Carolina Fresh Produce Co. | 9 | sent | `daniel+carolina-fresh-produce-co@…` |
| Foothills Organic Distribution | 8 | sent | `daniel+foothills-organic-distribution@…` |
| Three Rivers Beverage Co. | 4 | sent | `daniel+three-rivers-beverage-co@…` |
| Queen City Meats | 3 | sent | `daniel+queen-city-meats@…` |

Excluded (below `min_matches=2`): Tidewater Seafood (1 match), Charlotte Specialty Foods, Southern Harvest, Catawba Valley Bakery Supply, Piedmont Wholesale, Carolina Dairy (0 matches each).

15 distinct ingredient rows in `rfp_request_items` after wording-variant dedupe across the four distributors' scopes. Phase 5 compose cost: **$0.0458** (4 Claude calls, ~5300 input + ~2000 output tokens).

### Phase 5.1 re-run (rfp_request_id=2, 2026-05-14)

| Distributor | Matched (v1 → v1.1) | Ingredients in email | Notes |
|---|---:|---|---|
| Three Rivers Beverage Co. | 4 → **11** | 10 teas/juices/kombucha | v1's "caramelized garlic steak" leak (substring `tea` in `steak`) is gone; legit beverages surfaced |
| Carolina Fresh Produce Co. | 9 → **8** | 8 produce items | "Lime Cilantro Jalapeño Sauce" now in `unassigned_ingredients` (composite guard) |
| Foothills Organic Distribution | 8 → **7** | 7 produce items | same composite drop |
| Queen City Meats | 3 → **3** | 3 proteins | unchanged — no leaks here |

`unassigned_ingredients`: **86 items**, mostly the menu's in-house sauces and dressings (KBBQ dressing, balsamic vinaigrette, caesar dressing, garlic aioli, lime cilantro jalapeño sauce, miso sesame ginger dressing, pesto vinaigrette, charred jalapeño ranch, spicy cashew dressing, sweetgreen hot sauce, etc.), plus raw ingredients with no canonical distributor specialty in the seed roster (cucumbers, avocado, sweet potatoes, quinoa, white rice, parmesan crisps, focaccia, etc.). All four `dressing`/`sauce`/`vinaigrette`/`aioli` items in the unassigned list were former matcher-leak candidates.

Sample wholesale-unit conversions from the live `rfp_request_id=2` bodies: `Basil — ~12 bunches/week (~600 tbsp/week ≈ 12 bunches at planning density; please confirm your standard bunch size)`, `Shredded kale — ~56.2 lb/week (converted from cups at ~1 oz/cup chopped)`, `Kombucha — ~28.1 gallon/week (converted from fl oz; 128 fl oz/gallon)`. Lemon/lime juice are reported as `tsp/week` with the explicit "no wholesale rule applies — please quote in your standard unit" flag (the sanity fallback firing as designed; we don't have a teaspoon-volume rule because the planning density depends on whether it's freshly squeezed, bottled, or concentrate).

## Phase 6 — Inbox Monitor + Quote Parser + Follow-up Agent + Recommender

End-to-end loop closure: poll Gmail via IMAP → attribute inbound replies → parse with Claude → trigger at-most-one follow-up per distributor → compute a null-safe recommendation with explicit basket-coverage honesty.

### Failure-mode invariants (acceptance tests)

| # | Invariant | Enforcement |
|---|---|---|
| **F1** | Same UID processed twice persists exactly one `rfp_emails` row. | `UNIQUE(mailbox, uid_validity, uid)` on `imap_seen_uids` + same-transaction insert with `rfp_emails` (Amendment B). |
| **F2** | A reply with no Message-ID match, no plus-tag, no `[RFP-{id}]` subject is persisted with `attribution_method='unattributed'`, NOT dropped, NOT raised. | `attribute_reply` always returns an `AttributionResult`; the fallback tier returns method=`unattributed` with NULL `rfp_request_id`/`distributor_id`. |
| **F3** | For any `(rfp_request_id, distributor_id)`, at most one `rfp_emails WHERE is_followup=true`. | **DB-enforced** via `CREATE UNIQUE INDEX ix_one_followup_per_dist_rfp ... WHERE is_followup=true` (migration 0004, Amendment A). The follow-up agent's IntegrityError handler logs `cap_reached`. |
| **F4** | A follow-up send must not, in the same call stack, trigger another send. | Pipeline calls `maybe_send_followup` once per qualifying inbound; the agent itself does no recursion. Test asserts Resend POST count == 1 + Claude compose count == 1. |
| **F5** | A quote with `unit_price=NULL`, `delivery_days=NULL`, or `min_order_qty=NULL` must not crash the recommender and must not score the NULL as zero. | `_score_cost` excludes null-price ingredients from the basket sum AND flags `incomplete_comparison=true`. `_score_delivery` returns `0.0` (asymmetric — see below). `_score_moq` returns `0.5` (neutral). |
| **F6** | Quotes on `quantity=NULL` (TBD) items persist with the quoted unit price; the recommender excludes from basket sum and flags incomplete. | `_wholesale_quantity_for` returns `None` (not zero) when item.quantity is NULL; `_score_cost` treats this the same as null-price. |
| **F7** | One bad reply doesn't poison the batch. | `quote_pipeline.poll_and_process` wraps each `parse_quote_email` call in `try/except` and marks `parse_status='parse_failed'` on the offending row; the loop continues. |
| **F8** | IMAP connection / auth / network failure is non-fatal. | `poll_inbox` catches `imaplib.IMAP4.error`, `OSError`, `ssl.SSLError`, `RuntimeError`; returns `InboxPollResult(error=...)`. API endpoint returns 200 with `poll_error` set. |

**Mutation-tested.** For each F-invariant the offending guard was temporarily broken, the corresponding test was re-run, and the failure mode was confirmed before reverting. The tests are real assertions, not theatre.

### IMAP approach

Stdlib `imaplib.IMAP4_SSL` + `asyncio.to_thread` wrapper. No new dependency. Body extraction uses `email.message_from_bytes` (RFC-2822 canonical) and prefers `text/plain` parts, falling back to `bs4`-stripped HTML when only HTML is available. Headers like `Date`, `Message-ID`, `In-Reply-To`, `References` are read via `email.utils` helpers (no hand-rolled regex).

**Own-send filter.** Phase 5's demo recipient override (`daniel+slug@…`) means our outbound RFPs land in the same Workspace mailbox we poll. The monitor skips messages where `From == settings.rfp_from_email` and records the UID as seen so we don't re-fetch.

### Attribution (3 tiers, priority order)

1. **`In-Reply-To` / `References` headers** → exact match on `rfp_emails.message_id` where `direction='out'`. Primary signal. Sets `rfp_request_id`, `distributor_id`, `matched_rfp_email_id`.
2. **Plus-tag in `To` / `Delivered-To`** → derive distributor slug via `_normalize_slug`; match against `Distributor.name` slug; resolve to that distributor's latest open RFP.
3. **`[RFP-{id}]` subject prefix** → match `rfp_request_id` only; `distributor_id=NULL` (we know which RFP but not whom). Tier-3 attribution legitimately attributes to RFP only — the quote parser skips quote persistence when `distributor_id` is NULL because the comparison key needs both.

**Fallback:** `attribution_method='unattributed'` with both FKs NULL. Logged, not dropped, never crashes (F2).

### Quote parser (Claude `parse_quote` tool, stage='quote_parse')

Receives the inbound body + the per-distributor scoped ingredient list (NOT the union — scoping uses `specialty_tags_for(ingredient) & distributor.specialties`). Returns per-ingredient `{unit_price, unit, min_order_qty, delivery_days, terms, missing_fields[], parse_confidence}` plus `off_topic` and an optional `note`. Auto-responders / OOO / marketing replies set `off_topic=true` and return `quotes=[]`.

`_add_missing_from_nulls` augments `missing_fields` with any field Claude returned as NULL but forgot to flag — defensive against the parser under-reporting gaps.

### Follow-up agent

Triggered by the pipeline when a parsed reply has any non-empty `missing_fields`. Composes a scoped follow-up via the `compose_followup_email` Claude tool — asks only for fields the distributor omitted; never re-asks for fields already provided.

**Threading.** Follow-ups mint their own Message-ID (`<rfp-{req}-{dist}-fu1-{hex}@domain>` — `fu1` marker makes them greppable) and set `In-Reply-To` = the inbound reply's message_id so Gmail threads them under the distributor's response. Sent via Resend; persisted with `is_followup=true`.

**Termination.** A pre-flight `SELECT` against the existing follow-up cap saves a Claude+Resend round trip when the cap is already reached. The DB partial unique index is the load-bearing invariant — if the pre-flight is bypassed (concurrent inserts or a code-path change), the index still catches the second attempt and the agent logs `followup.skipped.cap_reached` from the IntegrityError handler. A still-incomplete reply to a follow-up does NOT trigger a second follow-up.

### Recommendation (`recommender.compute_for_rfp`)

**Trigger:** when all expected distributors have replied OR `rfp_request.deadline < now()` OR `force=True` (CLI / API explicit override).

**Scoring weights:**

```
score = 0.50 × cost_score
      + 0.20 × delivery_score
      + 0.15 × moq_fit_score
      + 0.15 × completeness_score
```

Each component normalized to `[0, 1]` (higher = better) before weighting. Persisted to `recommendations` table with `score`, `rationale`, `incomplete_comparison`, `coverage_pct`, `component_breakdown` (JSONB).

**Asymmetric null-safety (intentional — defended in the rationale text).**

- **`unit_price=NULL` OR `wholesale_quantity=NULL`** → that ingredient is **EXCLUDED** from the basket sum, AND the basket is flagged `incomplete_comparison=true`. *Reason:* we can't compute basket cost without both; silently scoring as zero would make the distributor look artificially cheap. Treating NULL as "absent data" is honest.
- **`delivery_days=NULL`** → scored **0.0** (WORST-CASE), NOT excluded, NOT median-imputed. *Reason:* a distributor refusing to commit to delivery is a real negative signal, not absent data. Distinct from price NULL on purpose: price NULL means "working on it"; delivery NULL means "won't commit". The rationale text repeats this verbatim so the writeup can defend the choice.
- **`min_order_qty=NULL`** → scored **0.5** (neutral). *Reason:* genuinely ambiguous — many distributors don't enforce one.

**Cross-distributor basket coverage.** `coverage_pct = (quoted_ingredient_count / requested_ingredient_count) × 100` is surfaced because distributors quote different subsets — the recommendation isn't always apples-to-apples. The rationale text explicitly calls this out: *"Carolina Fresh scored 0.96 on a basket of 8/21 requested items (coverage 38%); this score is not strictly apples-to-apples vs other distributors."*

If no distributor has a non-zero `cost_score` AND non-empty quotes, recommender persists `score=0.0` with rationale "no priced quotes received before deadline" — does not crash, does not pick arbitrarily.

### Schema delta — migration `0004_phase6_inbox`

| Table | Column | Type | Purpose |
|---|---|---|---|
| `rfp_emails` | `is_followup` | `BOOLEAN NOT NULL DEFAULT false` | F3 |
| `rfp_emails` | `attribution_method` | `VARCHAR(40)` NULL | Phase 6 audit |
| `rfp_emails` | `parse_status` | `VARCHAR(40)` NULL | F7 (`unparsed`/`parsed`/`parse_failed`) |
| `rfp_emails` | `rfp_request_id`, `distributor_id` | RELAXED to NULLABLE | F2 (unattributed rows) |
| `imap_seen_uids` (new) | id, mailbox, uid_validity, uid, seen_at, rfp_email_id, UNIQUE(mailbox, uid_validity, uid) | — | F1 idempotency |
| `recommendations` | `incomplete_comparison` | `BOOLEAN NOT NULL DEFAULT false` | basket-honesty flag |
| `recommendations` | `coverage_pct` | `NUMERIC(5,2)` NULL | per-distributor coverage |
| `recommendations` | `component_breakdown` | `JSONB` NULL | full ranked list + per-component scores |
| **partial UNIQUE INDEX** | `ix_one_followup_per_dist_rfp` on `rfp_emails (rfp_request_id, distributor_id) WHERE is_followup=true` | — | Amendment A — F3 DB-enforced |

### API surface added in Phase 6

```
POST /api/rfp/{id}/poll_inbox      # one poll cycle (force_recommendation optional)
POST /api/rfp/{id}/finalize        # force-compute recommendation regardless of deadline
GET  /api/rfp/{id}/recommendation  # current recommendation (computes if conditions are met)
GET  /api/rfp/{id}/quotes          # quotes grouped by distributor
GET  /api/rfp/{id}/comparison      # distributors × ingredients matrix
```

CLI:

```
python -m app.cli poll-inbox <rfp_request_id> [--force]
python -m app.cli finalize <rfp_request_id>
```

SSE substages: `inbox_poll:start|complete`, `quote_parse:start|complete|error`, `followup:start|complete`, `recommendation:start|complete`. The umbrella `quote_collection` stage wraps everything.

### Phase 6 verified run (rfp_request_id=1, 2026-05-14)

After Phase 5.1's `<rfp-2-*>` send, the test suite truncates DB state between every test (necessary for isolation), so a fresh demo run regenerated `rfp_request_id=1` with the same 4 distributors. Four simulated distributor replies were sent via Gmail SMTP using daniel's app password (one-off harness `/tmp/simulate_replies.py`, since the user's manual replies referenced the now-vanished `rfp-2-*` Message-IDs):

| Distributor | Reply type | Parse result |
|---|---|---|
| Carolina Fresh Produce Co. | Complete quote, 8 produce items | 8 quotes, 0 missing fields |
| Foothills Organic Distribution | Prices only, no delivery_days/terms | 7 quotes, 14 missing fields → **follow-up sent** |
| Queen City Meats | Chicken priced, steak null-price, bacon declined | 2 quotes, 1 with null price |
| Three Rivers Beverage Co. | OOO auto-responder | `off_topic=true`, 0 quotes |

**Recommendation** (force=true): **Carolina Fresh Produce Co. — score 0.96** on a basket of 8/21 requested items (coverage 38%). Rationale calls out the coverage gap explicitly. Foothills second at 0.74 (penalized by delivery_days=NULL across all 7 items — the asymmetric rule firing in production). Queen City third at 0.19 (low coverage + null-price exclusion + delivery NULL).

**F1 verified live:** second `poll-inbox` invocation returned `inbound_count=0`, `duplicate_uids_skipped=24` — all previously fetched UIDs blocked at the unique constraint.

## Phase 7 — Demo orchestration + Next.js UI

### Part A — `make demo`

One command runs the pipeline end-to-end against an empty DB and leaves it in a coherent demo-ready state:

```
make demo          # idempotent — no-ops if an RFP already exists
make demo-reset    # TRUNCATEs demo content rows, then re-runs
make poll          # poll-inbox on the most recent RFP
make finalize      # force-compute the recommendation
```

The CLI command is `python -m app.cli run-demo [--reset-data] [--yes]`. Each step is guarded by a count check so a partial re-run picks up where it left off (skips parse if dishes exist, skips enrich if every ingredient has an FDC id, etc.). Two helper commands `latest-rfp`, `poll-latest`, `finalize-latest` auto-discover the most recent `rfp_request_id` for the demo restaurant.

**Idempotency contract:**

| Pre-state | `make demo` behavior |
|---|---|
| Empty DB | Full pipeline (seed → parse → enrich → discover → send_rfps) |
| Mid-pipeline (e.g. dishes exist, no RFP) | Resumes from the first incomplete step |
| Already-populated (RFP exists) | No-op, prints summary with existing IDs |

**Reset:** `make demo-reset` (or `--reset-data --yes`) runs a **single TRUNCATE ... CASCADE** statement against the demo content tables. The statement explicitly lists every dependent table (`rfp_requests`, `rfp_request_items`, `rfp_emails`, `imap_seen_uids`, `quotes`, `recommendations`, `dishes`, `dish_ingredients`, `ingredient_prices`, `ingredients`, `llm_usage`) so PostgreSQL resolves all FK dependencies atomically — no half-completed truncation. **`distributors` and `restaurants` are preserved** (seed roster + idempotent restaurant row). **The schema is never touched** — Alembic migrations stay applied.

### Part B — Next.js UI (frontend/)

Next.js 14 App Router + TypeScript + Tailwind, single-page pipeline visualization that renders server-side against the live backend. The page is one cohesive vertical scroll through all six pipeline stages with a sticky cost-dashboard header and sticky pipeline-triggers footer.

**Design tokens (defined, not default Tailwind):** zinc backbone (`ink` palette), one accent (`emerald`), dark mode only (`color-scheme: dark` + `darkMode: "class"` on `<html>`). Numbers in `font-mono` (`.num` utility) so prices and IDs align. Tight type scale — no `text-lg` (everything in between looks soft). Generous whitespace (`py-12` between stage cards). No marketing shadows; `rounded-md` everywhere. Subtle emerald glow at the top of the page lifts the header without competing for attention.

**Stage components** (one per pipeline step, server-rendered for SSR speed):

1. **`StageMenu`** — dish cards in a responsive grid; per-dish `parse_confidence` badge (traffic-light: green ≥ 0.8 / amber 0.5–0.8 / rose < 0.5); ingredient chips with their own per-ingredient `estimation_confidence` badges. The above-and-beyond surface for the parser's honest confidence scoring.
2. **`StagePricing`** — sortable table of FDC-matched ingredients with latest price/unit and 30-day trend. Trend arrow is **asymmetric**: rising price = rose (bad for the buyer), falling = emerald, flat = neutral. Rows with `pricing_unavailable=true` render an explicit "no AMS feed" pill — never imputed.
3. **`StageDistributors`** — distributor cards with specialty chips, match score, distance, and a `source` badge (`seed` / `google_places` / `google_places_merged`). Sorted by match count descending; controls (0 matches) rendered at lower opacity but kept visible.
4. **`StageRfpEmails`** — clickable list of outbound RFPs (one per distributor); click opens a modal with the full Claude-composed body, the minted `Message-ID`, the actual `daniel+slug@...` recipient, and the nominal `.example` placeholder. Follow-ups grouped separately. The `unassignedIngredients` count is rendered as an amber callout — *"86 ingredients unassigned — mostly in-house preparations + items outside the distributor cohort"* — with an expandable full list. Honest gap, surfaced loudly.
5. **`StageComparison`** — distributor × ingredient matrix, **grouped by FDC category** so the per-distributor basket is legible (produce rows together, protein rows together, etc.). Each column header carries a coverage chip (`8/21 · 38%`) — green ≥ 80%, amber ≥ 40%, neutral below. Empty cells (no quote received) are visually distinct from cells with a NULL price (amber "no price" pill).
6. **`StageRecommendation`** — pick card with the distributor name, score, coverage_pct, and the `incomplete_comparison` callout rendered as an explicit amber explanation. Four component progress bars (cost/delivery/MOQ/completeness) with weight × normalized score; null-imputed components colored amber instead of emerald so the asymmetric null-safety rule is visible. Rationale text below, in italic. Runners-up collapsed below the pick.

**Empty states for stages 5 & 6** (per amendment): after `make demo` but before any quotes have been collected, these stages render polished `EmptyState` cards:

- Stage 5: *"Awaiting distributor quotes. Reply to the RFP emails, then click **Poll Inbox** below to parse the replies. This panel will populate as quotes arrive."*
- Stage 6: *"Recommendation pending — collect quotes first."* (with a contextual hint from `not_ready_reason`).

When the user triggers Poll Inbox or Finalize from the footer, the page calls `router.refresh()` after the request completes, re-fetching all server components and re-rendering Stages 4–6 in place. **No page jank, no flash.** That's the core Loom moment — the agentic loop happening on camera.

**Cost dashboard** (sticky upper-right): reads `/api/usage`, shows total `$X.XX` in emerald + per-stage breakdown bars with call counts. Persistent so the viewer can watch it tick up as poll/finalize fires.

**SSE live events** (sticky footer): subscribes to `GET /api/restaurants/{id}/events` via the browser `EventSource` API (no library). Listens for the full set of stage:status pairs the backend emits and renders the last 4 as inline pills in the footer. A small green dot pulses while connected; greys when disconnected. **Fallback:** the trigger buttons always force a `router.refresh()` regardless of SSE delivery, so even if events drop the UI still updates.

**Backend addition (single small route):** `GET /api/restaurants/{id}` for the header to render the restaurant name + location. No schema change.

**Docker:** `frontend/Dockerfile` is a multi-stage `node:20-alpine` build using Next.js's `output: "standalone"`. `docker-compose.yml` brings up `db`, `backend`, `frontend` together; secrets are read from `.env` at the compose root. `NEXT_PUBLIC_API_BASE_URL` defaults to `http://localhost:8000` for local dev and `http://backend:8000` inside the compose network (overridable per environment via `.env.local.example`).

### Phase 7 verified run (2026-05-14)

`make demo` against an empty DB produced:

| Table | Rows |
|---|---:|
| restaurants | 1 |
| dishes | 45 |
| dish_ingredients | 267 |
| ingredients | 107 |
| ingredient_prices | 2,102 |
| distributors | 10 |
| rfp_requests | 1 |
| rfp_request_items | 34 |
| rfp_emails | 5 |
| quotes / recommendations | 0 / 0 (awaiting replies) |
| llm_usage | 7 calls / $0.34 |

5 RFPs sent to `daniel+<slug>@getserviceledger.com` (Piedmont Wholesale, Southern Harvest, Carolina Fresh Produce, Charlotte Specialty Foods, Catawba Valley Bakery Supply — top-5 distributors above `min_matches=2`). Frontend `npm run build` and `npm run start` both clean; SSR render against the live backend rendered all 6 stage headings, the Carolina Fresh card content, the cost dashboard, and the empty states for Stages 5 & 6.

## Restaurant Choice

**Sweetgreen — Park Road Shopping Center, 4329 Park Rd, Charlotte, NC 28209.** Public HTML menu snapshot saved to `data/menus/sweetgreen.html`. Chosen because the menu is ingredient-forward (each dish lists its components), which exercises the parser's "high-confidence ingredient extraction" path well.

---

**Spec maintained by Claude Code — update as decisions evolve.**