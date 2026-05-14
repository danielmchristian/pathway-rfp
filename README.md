# Pathway RFP

End-to-end pipeline that ingests a restaurant menu and produces a recommended distributor with quotes. Five stages: menu parsing → USDA ingredient enrichment → distributor discovery → RFP email send → inbox monitoring with quote parsing, follow-up, and a scored recommendation. The pinned restaurant is Sweetgreen — Park Road Shopping Center, Charlotte NC.

The full design document lives at [`docs/spec.md`](docs/spec.md); this README is a runbook plus the reasoning behind the more interesting decisions.

## Architecture

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
                           | USDA Enrichment  |
                           |  FDC + AMS       |
                           +------------------+
                                    |
                                    v
                           +------------------+
                           | Distributor      |
                           | Discovery        |
                           | (seed + Places)  |
                           +------------------+
                                    |
                                    v
                           +------------------+
                           | RFP Composition  |
                           | + Send (Resend)  |
                           +------------------+
                                    |
                                    v
                           +------------------+
                           | Inbox Monitor +  |
                           | Quote Parser +   |
                           | Follow-up +      |
                           | Recommender      |
                           +------------------+

UI (Next.js) <---- SSE stream + REST ---- FastAPI
```

**Tech stack.** FastAPI · SQLAlchemy 2.x async · Alembic · PostgreSQL 16 · Anthropic Claude (`claude-sonnet-4-6`, tool-use for structured outputs) · Resend (outbound email) · stdlib `imaplib` over Gmail IMAP (inbound) · USDA FoodData Central + Agricultural Marketing Service · Google Places (optional) · Next.js 14 App Router + Tailwind · Docker Compose · `uv` for Python deps · `ruff` for lint/format · pytest with a fenced-off `pathway_test` DB.

**Core principles.**

- **The database is the source of truth.** Every stage reads from and writes to Postgres. Pipeline state can always be reconstructed from DB rows.
- **Each stage is independently runnable** — `make demo` runs them all in sequence, but you can re-trigger any single stage via CLI (`python -m app.cli …`) or REST without re-running the others.
- **Structured LLM outputs only.** Every Claude call uses tool-use with a strict JSON schema. We never parse free-form prose.
- **Confidence scores travel with the data** — `parse_confidence` on dishes, `estimation_confidence` on dish/ingredient links, `parse_confidence` on quotes — surfaced as traffic-light badges in the UI.
- **In-memory pub/sub event bus** drives the streaming pipeline UI. Events are not persisted; they're reconstructible from DB state if needed.

## Setup

Two ways to bring it up. The host-run flow is what we use day-to-day; `docker compose up` exists for a one-shot demo.

### Prerequisites

- Docker (Postgres runs in a container regardless of which flow you pick)
- Python 3.11+ + [`uv`](https://docs.astral.sh/uv/) for the backend
- Node 20+ for the frontend
- API keys (see [Environment variables](#environment-variables))

### Host-run dev (recommended)

```bash
cp .env.example .env                  # fill in API keys + IMAP credentials
make setup                            # uv sync — installs deps, writes uv.lock
make db-up                            # docker-compose up -d db; waits for healthy
make db-migrate                       # alembic upgrade head
make demo                             # runs the full pipeline end-to-end
make dev                              # uvicorn on :8000
# in another shell:
cd frontend && npm install && npm run dev   # Next.js on :3000
```

### All-in-one Docker

```bash
cp .env.example .env
docker compose up                     # db + backend + frontend
# backend: localhost:8000, frontend: localhost:3000
```

### The demo loop

`make demo` is idempotent — it no-ops if an RFP already exists. Reset with `make demo-reset` (preserves distributors + restaurant + schema; truncates content tables atomically).

```bash
make demo            # parse menu → enrich → discover → send 5 RFP emails
# … reply to the RFPs (see below) …
make poll            # poll inbox, parse quote replies, send follow-ups if needed
make finalize        # compute recommendation (force=true)
```

**How to reply during the demo.** The 5 RFP emails are sent to `daniel+<distributor-slug>@getserviceledger.com` via Resend, with `From: procurement@getserviceledger.com`. Plus-addressing means all five land in one Gmail inbox you can poll. The inbox monitor attributes replies via three tiers (most specific first):

1. **`In-Reply-To` header** → `rfp_emails.message_id`. Use this by hitting **Reply** in Gmail on the original RFP — Gmail auto-populates `In-Reply-To` from our minted `Message-ID`.
2. **Plus-tag in `To`/`Delivered-To`** → distributor slug → distributor row. Use this by composing a *fresh* email to `daniel+carolina-fresh-produce-co@getserviceledger.com` (or any of the 5 slugs in `rfp_emails.recipient_actual`).
3. **`[RFP-{id}]` subject prefix** → RFP only. Last-resort fallback; sets `rfp_request_id` but leaves `distributor_id=NULL`, so the quote parser skips persistence (you can't compare quotes without knowing who they're from).

Tier 1 or tier 2 both produce a clean `distributor_id` → quote persistence → recommendation. Tier 3 is the honest "we know what RFP this is for but not who sent it" case.

### Environment variables

| Key | Required for | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | every Claude call | [console.anthropic.com](https://console.anthropic.com) |
| `USDA_FDC_API_KEY` | FDC food identity matching | [fdc.nal.usda.gov/api-key-signup](https://fdc.nal.usda.gov/api-key-signup) — free |
| `USDA_AMS_API_KEY` | AMS wholesale prices (optional — seed fallback exists) | [mymarketnews.ams.usda.gov](https://mymarketnews.ams.usda.gov/) — free signup |
| `GOOGLE_PLACES_API_KEY` | optional Places-based distributor discovery | Google Cloud Console |
| `RESEND_API_KEY` | outbound RFP email | [resend.com](https://resend.com/) — domain must be verified |
| `RFP_FROM_EMAIL` | outbound From (and Reply-To) | a verified address on a Resend-verified domain |
| `RFP_DEMO_INBOX` | base address for plus-addressed demo recipients | a Google Workspace mailbox that accepts plus-addressing |
| `IMAP_USER` / `IMAP_PASSWORD` | inbox polling | the same Gmail mailbox; password is a Google **app password**, not the account password |
| `COVERS_PER_DISH_PER_WEEK` | weekly demand estimate | optional; default 150 |

`DATABASE_URL` defaults to the docker-compose Postgres and doesn't need to be set unless you point at a different DB.

## Pipeline stages

**1 · Menu parser.** Fetches the HTML (or a PDF, or a photographed menu) and calls Claude with the `extract_menu_items` tool. Returns one row per dish into `dishes`, deduplicated ingredient rows into `ingredients` (upsert on `normalized_name`), and the join into `dish_ingredients` — each with its own `parse_confidence` / `estimation_confidence`. Vague items ("Chef's Special") flag below 0.7 and surface as amber chips in the UI.

**2 · USDA enrichment.** Two USDA endpoints behind one stage. **FDC** (`api.nal.usda.gov/fdc/v1`) handles food *identity* — `POST /foods/search`, exclude the `Branded` dataType (snack products pollute the search), top-5 hits. Auto-accept the top hit if `score ≥ 200` OR `top/second ≥ 1.5`; otherwise Claude disambiguates via the `pick_fdc_match` tool. **AMS Market News** (`marsapi.ams.usda.gov`, HTTP Basic auth) handles wholesale prices from the Atlanta Terminal (slug 2278 — closest to Charlotte). Unmatched ingredients (proteins, oils, nuts, grains — AMS doesn't cover them) get one `pricing_unavailable=true` sentinel row, an honest gap. A 30-day price series + trend (up/down/flat/unknown by ±3% delta) feeds the UI's Stage 2 panel. Live AMS failures fall back automatically to `data/seed_ams_prices.json` with `source='ams_seed_fallback'`.

**3 · Distributor discovery.** Seed roster is the **primary** source: 10 curated Charlotte/Gastonia NC distributors in `data/distributors_seed.json`, with realistic addresses, lat/long in actual industrial corridors, `.example` email placeholders, and a deliberately calibrated specialty mix (8 of 10 overlap with the Sweetgreen ingredient set; 2 are intentional non-overlaps — `Tidewater Seafood`, `Three Rivers Beverage Co.`). Google Places is **optional enrichment** gated on `GOOGLE_PLACES_API_KEY`: two `places:searchNearby` calls (50km radius from the restaurant), then a Claude pass (`classify_distributor_candidates`) filters retail chains, individual restaurants, and other non-wholesale results. On our last demo: Places returned 24 candidates, 24 rejected as retail noise. Merges with seed entries are conservative — seed wins authoritatively on name + address + lat/long; Places wins on phone + email + website.

**4 · RFP email composition + send.** A matcher (`distributor_matching.specialty_tags_for`) maps each ingredient's FDC category + name hints to a tag set, intersected against each distributor's specialties. The aggregator collapses wording variants per distributor (stripping `organic`/`fresh`/`shredded` etc. and conservative plural endings, summing quantities only when units agree), then converts per-serving units to wholesale units (`tbsp` of herbs → `bunch`; `cup` of leafy greens → `lb`; `fl oz` → `gallon`; etc.) with explicit conversion notes in the email body. Claude composes one email per distributor scoped to their matched ingredients only via the `compose_rfp_email` tool. Resend sends with a minted RFC-822 `Message-ID` of the form `<rfp-{req}-{dist}-{8hex}@…>` — that's the key Phase 6's reply matcher uses. Each send is persisted to `rfp_emails` with both `recipient_actual` (the `daniel+slug@…` address) and `recipient_nominal` (the distributor's `.example` placeholder) so the audit trail never lies about who we *would* have emailed vs. who we *did*.

**5 · Inbox monitor + quote parser + follow-up + recommender.** Stdlib `imaplib.IMAP4_SSL` over Gmail (no new dep) wrapped in `asyncio.to_thread`. SEARCH UNSEEN, FETCH bodies, attribute via the three-tier strategy above, persist each inbound to `rfp_emails` *atomically* with its `imap_seen_uids` row (Amendment B — UNIQUE on `(mailbox, uid_validity, uid)` + same-tx insert means a crash between FETCH and persist can't lose data, and a re-poll of the same UID can't duplicate it). Claude parses each attributed reply via the `parse_quote` tool, scoped to the *per-distributor* ingredient list (not the union). Replies with non-empty `missing_fields[]` trigger one follow-up via the `compose_followup_email` tool — capped at one per `(rfp, distributor)` by a **partial unique index** (`CREATE UNIQUE INDEX … WHERE is_followup=true`), not just an application check. Once all expected distributors have replied or the deadline passes or `force=True`, the recommender runs.

## Design decisions

**Structured outputs via tool-use, everywhere.** Every Claude call defines a JSON Schema via the Anthropic tool-use API and reads the result via `tool_use.input`. No regex, no `json.loads(response.text)`, no "extract the JSON from this prose." A response that doesn't tool-call is treated as a parse failure. Tool definitions live in `app/llm/tools.py`.

**Confidence scores throughout.** Every probabilistic step — parser, FDC matcher, quote parser — emits a confidence and it's persisted. The frontend renders traffic-light badges (green ≥ 0.8, amber 0.5–0.8, rose < 0.5). The point isn't to *be* confident; it's to be **calibrated** about when we aren't.

**USDA FDC vs AMS — different APIs, different jobs.** FDC publishes *food identity* and nutrition; it does **not** publish prices. Wholesale pricing lives in the separate AMS Market News API. We use both — FDC for canonical identity and category, AMS for actual $/unit at the Atlanta Terminal. The AMS endpoint is gated on a free registration with HTTP Basic auth (API key as the username), occasionally times out, and doesn't cover proteins/oils/nuts/grains. We document that gap with explicit `pricing_unavailable=true` rows instead of imputing zero or skipping silently.

**Composite-ingredient guard — two iterations, honestly.** Sweetgreen makes its dressings, sauces, and crunchy toppings in-house. Routing "Lime Cilantro Jalapeño Sauce" or "Charred Jalapeño Ranch Dressing" to a raw-produce distributor invents a supply relationship that doesn't exist. **Phase 5.1** added a composite-name regex covering `sauce`/`dressing`/`vinaigrette`/`aioli`/`pesto`/`hummus`/`tahini`/`mayo` etc. **Phase 5.2** caught the gaps that pass missed once a real menu was loaded — `ranch`, `slaw`, `crunch`, `crumble`, `crisps`, `kimchi`, `seasoning`, `caramel`, plus the discovery that the FDC category `'Soups, Sauces, and Gravies'` was *category-mapping* sauces to dry-goods distributors even when the *name* guard didn't fire. The category map now returns `[]` for that bucket; routing only raw goods is the right invariant. Composites land in `unassigned_ingredients` — surfaced loudly in the UI as an amber callout, not silently dropped.

**Google Places noise filter — $0.03 to drop 24 retail chains.** A naive Places query for "wholesale food distributor" returns Harris Teeter, Costco, Sam's Club, individual restaurants, and a long tail of non-food businesses tagged with "wholesale." A Claude pass over the batch (`classify_distributor_candidates`, stage `distributor_filter`) returns `{is_wholesale_distributor, reason}` per candidate. Last demo run: 24 candidates in, 0 kept. The cost is logged so the trade-off is visible.

**Three-tier email attribution.** `In-Reply-To` is the primary signal (set automatically when a recipient hits Reply). Plus-tag in `To` / `Delivered-To` survives clients that strip threading headers, and supports fresh compositions to `daniel+<slug>@…`. `[RFP-{id}]` subject prefix is last-resort: it sets the RFP but leaves `distributor_id=NULL` because the quote parser deliberately skips persistence in that case (an unattributed quote can't be compared). Anything without a match attributes as `unattributed` — **persisted, not dropped, not raised** (invariant F2; auto-responders and OOO replies land here too).

**Follow-up termination — DB-enforced, not app-enforced.** A `(rfp_request_id, distributor_id)` is allowed at most one `is_followup=true` row, enforced by a **partial unique index** (`migration 0004`). A pre-flight `SELECT` in the agent saves a Claude+Resend round trip when the cap is already reached, but the index is the load-bearing invariant: even if the pre-flight is bypassed (concurrent inserts, code-path drift), the `IntegrityError` handler logs `followup.skipped.cap_reached` and the agent terminates. A still-incomplete reply to a follow-up does **not** trigger a second follow-up.

**Asymmetric null-safety in recommendation scoring.** Different NULLs mean different things and the recommender treats them differently — and the *rationale text* explains the asymmetry verbatim so the writeup can defend it.

- `unit_price=NULL` *(or `wholesale_quantity=NULL`)* → ingredient excluded from comparison; basket flagged `incomplete_comparison=true`. Price NULL means "they're working on it" — absent data.
- `delivery_days=NULL` → scored **0.0** (worst-case). Delivery NULL means "they won't commit" — that's a real negative signal, not absent data.
- `min_order_qty=NULL` → scored **0.5** (neutral). Genuinely ambiguous; many distributors don't enforce one.

**Cost is scored per-item, not by basket total.** This was a real bug we caught on the first live finalize, fixed in this session. The initial recommender summed `unit_price × wholesale_quantity` to a basket total per distributor and ranked totals: lowest = 1.0, highest = 0.0. That silently rewarded incompleteness — a distributor who quoted fewer items had a smaller basket sum, so the scorer made them look "cheaper." On rfp_request_id=1, a 5-item partial reply with `"I'll get back to you on the rest"` ranked **above** a 9-item complete competitive reply. The fix: rank distributors *per ingredient* on `unit_price` (cheapest=1.0, most expensive=0.0); each distributor's cost score is the mean of their per-item ranks across the ingredients where direct comparison was possible. Distributors with no comparable items get 0.5 (neutral — uncomparable, not punished). Coverage was promoted from a footnote in the rationale to a **first-class 20%-weight component**. New weights: `cost 0.35 / delivery 0.20 / moq 0.10 / completeness 0.15 / coverage 0.20`. Two new regression tests pin both directions of the trade-off (`test_complete_competitive_outranks_partial_equivalent`, `test_partial_with_strictly_better_per_item_pricing_can_still_win`).

**Atomic inbox-poll idempotency.** Re-polling the same UID can't insert a duplicate, and a crash between FETCH and `INSERT rfp_emails` can't lose the UID. The `imap_seen_uids` table has a `UNIQUE(mailbox, uid_validity, uid)` and is written **in the same transaction** as the `rfp_emails` row (Amendment B). `UIDVALIDITY` is stored alongside `UID` so a mailbox UID reset doesn't make us think a brand-new email is one we've seen before. Verified live: a second `poll-inbox` returned `inbound_count=0, duplicate_uids_skipped=N` — all previously-fetched UIDs blocked at the unique constraint (invariant F1).

**Test database isolation — fenced off at collection time.** pytest's autouse `_reset_state` fixture issues `TRUNCATE TABLE … RESTART IDENTITY CASCADE` on 11 tables between tests for isolation. For the first half of the project that fixture inherited the dev `.env`'s `DATABASE_URL` and silently wiped the demo data every `make test`. We caught it after three unexplained wipes. The fix: `backend/tests/conftest.py` rebinds `DATABASE_URL` to `pathway_test` *before any `app.*` import*, auto-creates and migrates the test DB on first run (`pytest_configure`), and **asserts at module top** that `engine.url` ends in `/pathway_test` — refusing to load the test suite otherwise. A noisy failure at collection is the right failure mode; the old silent wipe wasn't. Proof of durability: `make demo` → snapshot dev counts → `make test` (99 pass) → re-snapshot → empty `diff`.

## Real-world messiness handled

- **Vague menu items** like *"Chef's Special"* or seasonal placeholders: parser emits `parse_confidence < 0.7` rather than guessing ingredients, and the UI shows the amber badge so the operator can review before sending RFPs.
- **USDA AMS date quirks.** AMS returns dates in `MM/DD/YYYY` (with leading zeros sometimes, sometimes not) — parsed via `dateparser`, not a hand-rolled regex. AMS also occasionally returns empty bodies or 5xx; live failures auto-fall-back to `data/seed_ams_prices.json` with `source='ams_seed_fallback'` so persisted rows are honestly distinguishable from live data.
- **Unattributed replies.** Gmail admin emails, OOO auto-responders, marketing replies — none have a matching `In-Reply-To`/plus-tag/subject prefix. They're persisted with `attribution_method='unattributed'`, both FKs NULL, and the quote parser skips them. They are **not** dropped and they do **not** crash the loop (invariant F2).
- **Partial / prose quotes.** Real distributor replies are full of *"I can do $X but let me get back to you on delivery"*-style fragments. The `parse_quote` tool returns a strict-typed object with `unit_price`/`unit`/`min_order_qty`/`delivery_days`/`terms` (each nullable) plus a `missing_fields[]` array — and a defensive `_add_missing_from_nulls` pass augments `missing_fields` with anything Claude returned as NULL but forgot to flag. Missing fields trigger one follow-up; a still-incomplete reply to a follow-up doesn't trigger a second one.
- **Auto-responders.** The parser tool has an explicit `off_topic` bool. OOO and marketing replies set `off_topic=true` and return empty quotes — persisted as inbound, parsed without crashing, **never** treated as a quote.
- **Different baskets per distributor.** Carolina Fresh and Three Rivers Beverage quote different items; comparing their basket totals is apples-to-oranges. Coverage is surfaced both as a percentage in the rationale and as a 20%-weighted component in the score (see the per-item cost fix above).
- **Edge-case ingredients.** "SCOBY (probiotic culture)" and "yeast" and "salt" don't have AMS commodity codes; "kombucha" can't be priced as a raw ingredient. They land in `unassigned_ingredients` or get `pricing_unavailable=true` rows — surfaced loudly in the UI rather than silently dropped.

## Testing

99 tests, all pass; ~62s to run.

```bash
make test            # pytest, runs against pathway_test
make lint            # ruff check + ruff format --check
make fmt             # ruff format (writes)
```

The tests are organized around **failure-mode invariants** (F1–F8), each of which is enforced by a load-bearing mechanism (DB constraint, atomic transaction, try/except boundary) and then mutation-tested — the offending guard was temporarily broken, the test re-run to confirm it fails, then the guard restored. They're real assertions, not theatre.

| # | Invariant | Enforcement |
|---|---|---|
| F1 | Same UID never processed twice | `UNIQUE(mailbox, uid_validity, uid)` + same-tx insert with `rfp_emails` |
| F2 | Unmatchable reply persists as `unattributed`, never dropped, never raised | `attribute_reply` always returns; tier-4 fallback method |
| F3 | At most one follow-up per `(rfp, distributor)` | Partial unique index `WHERE is_followup=true` |
| F4 | A follow-up send doesn't recursively trigger another | Pipeline calls agent once per qualifying inbound; agent does no recursion |
| F5 | NULL price / delivery / MOQ don't crash recommender | Asymmetric null-safety (excluded vs 0.0 vs 0.5) |
| F6 | TBD-quantity items quote successfully but excluded from basket | `_wholesale_quantity_for` returns None, not 0 |
| F7 | One bad reply doesn't poison the batch | `parse_quote_email` wrapped per-row; `parse_status='parse_failed'` set |
| F8 | IMAP auth/network failure is non-fatal | Caught: `IMAP4.error`, `OSError`, `SSLError`, `RuntimeError` |

The autouse `_reset_state` fixture runs against a dedicated `pathway_test` database that's created automatically on first run and migrated to `alembic head`. A collection-time assert refuses to run the suite if it's somehow bound to the dev DB — see [Design decisions § Test database isolation](#design-decisions).

## Cost

Anthropic spend is tracked per call (`llm_usage` table) and aggregated into the UI's sticky cost dashboard. A full demo run with one inbound poll + one finalize costs roughly $0.40–$0.50.

| Stage | Calls (typical) | Cost |
|---|---:|---:|
| `menu_parse` | 1 (one menu) | ~$0.21 |
| `quote_parse` | 1 per inbound | ~$0.02 each |
| `rfp_compose` | 1 per distributor sent | ~$0.02 each |
| `followup_compose` | 1 per distributor w/ missing fields | ~$0.01 each |
| `distributor_filter` | 1 per Places search | ~$0.03 |
| **Total** (current DB after one full loop) | 18 calls | **$0.49** |

Cost tracking is built into the `traced_call` wrapper around the Anthropic SDK — every successful call writes a row before the calling transaction commits (using a separate session, so usage is logged even when the caller rolls back). Get the rollup via `GET /api/usage`.

## What I'd do with more time

- **Tighter ingredient matching.** The current demo leaves ~70 ingredients in `unassigned_ingredients` — mostly in-house preparations (the composite guard is working as designed), but a meaningful chunk are raw ingredients with no canonical specialty in the seed roster (cucumbers, avocado, sweet potatoes, quinoa). A larger specialty taxonomy + a couple of new distributor archetypes would close most of the gap.
- **Real distributor discovery at scale.** The seed roster is curated and the Places integration is a starting point; a production version would crawl Yelp/HouzPro/regional B2B directories, cluster duplicates across sources, and surface contact-info freshness as a confidence signal.
- **Multi-round negotiation.** The follow-up agent caps at one round by design — it's a price-discovery loop, not a negotiation. A real version would handle counter-quotes, alternative units ("we don't sell by the bunch — we sell by the case"), and substitution offers.
- **Deployment hardening.** No auth, no SSL, single-tenant, secrets in `.env`. The data model anticipates multi-tenancy via `restaurant_id` everywhere, but everything else is dev-grade.
- **Parser eval set.** The spec lists a stretch goal (`make eval`) for a hand-curated set of menus + expected parser outputs, run as a smoke test. Never built. Would buy real confidence in the calibration of `parse_confidence`.
- **Richer trend visualization.** The pricing trend (`up`/`down`/`flat`/`unknown`) is computed but rendered as a single arrow — a sparkline over the 30-day series would be more useful, and *seasonal* trend separation (vs. 30-day) would matter for actual procurement.

## Repository layout

```
pathway-rfp/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── models/        # SQLAlchemy 2.x ORM
│   │   ├── routers/       # FastAPI route handlers
│   │   ├── services/      # menu_parser, usda, distributor_*, email_sender,
│   │   │                  # inbox_monitor, quote_parser, followup_agent,
│   │   │                  # recommender, quantity_aggregator
│   │   ├── llm/           # Anthropic client + tool defs + traced_call
│   │   ├── pipeline/      # event bus + orchestration helpers
│   │   └── cli.py         # Typer entrypoints (run-demo, poll-inbox, finalize, …)
│   ├── alembic/
│   ├── tests/             # 99 tests, pathway_test DB
│   └── pyproject.toml
├── frontend/              # Next.js 14 App Router + Tailwind, dark-mode only
├── data/
│   ├── menus/             # Pinned Sweetgreen HTML snapshot
│   ├── distributors_seed.json
│   └── seed_ams_prices.json
├── docs/spec.md           # Full design doc
├── docker-compose.yml
├── Makefile
└── .env.example
```

See [`docs/spec.md`](docs/spec.md) for the long-form design narrative, full schema, F-invariant table, and per-phase verified-run snapshots.
