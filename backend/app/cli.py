"""Pathway RFP terminal CLI. Invoke with `python -m app.cli ...`."""

from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

import typer
from sqlalchemy import func, select, text

from app.db import SessionLocal
from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant
from app.models.rfp import RfpRequest
from app.services.distributor_discovery import discover_distributors
from app.services.ingredient_enrichment import enrich_restaurant
from app.services.menu_parser import parse_menu
from app.services.quote_pipeline import poll_and_process
from app.services.recommender import compute_for_rfp
from app.services.rfp_pipeline import send_rfps

app = typer.Typer(add_completion=False, no_args_is_help=True)

SWEETGREEN = {
    "name": "Sweetgreen — Park Road Shopping Center",
    "address": "4329 Park Rd",
    "city": "Charlotte",
    "state": "NC",
    "zip": "28209",
    "latitude": Decimal("35.1800"),
    "longitude": Decimal("-80.8380"),
    "menu_source_url": "https://order.sweetgreen.com/",
}


@app.command()
def seed_sweetgreen() -> None:
    """Create (or fetch) the Sweetgreen Charlotte restaurant row and print its id."""

    async def _run() -> int:
        async with SessionLocal() as session:
            existing = (
                await session.execute(
                    select(Restaurant).where(Restaurant.name == SWEETGREEN["name"])
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id
            r = Restaurant(**SWEETGREEN)
            session.add(r)
            await session.commit()
            await session.refresh(r)
            return r.id

    rid = asyncio.run(_run())
    typer.echo(json.dumps({"restaurant_id": rid, **SWEETGREEN}, default=str))


@app.command()
def parse(
    restaurant_id: int = typer.Argument(..., help="Restaurant ID from seed_sweetgreen / API"),
    menu_path: Path = typer.Argument(..., help="Path to .html or .txt menu file"),
) -> None:
    """Run the menu parser stage for an existing restaurant."""

    async def _run() -> None:
        result = await parse_menu(restaurant_id=restaurant_id, menu_path=menu_path)
        typer.echo(json.dumps(result.to_dict()))

    asyncio.run(_run())


@app.command()
def enrich(
    restaurant_id: int = typer.Argument(..., help="Restaurant ID"),
) -> None:
    """Run FDC matching + AMS pricing for every ingredient on the restaurant's menu."""

    async def _run() -> None:
        result = await enrich_restaurant(restaurant_id=restaurant_id)
        typer.echo(json.dumps(result.to_dict()))

    asyncio.run(_run())


@app.command()
def discover(
    restaurant_id: int = typer.Argument(..., help="Restaurant ID"),
) -> None:
    """Load seed distributors + optional Google Places enrichment."""

    async def _run() -> None:
        result = await discover_distributors(restaurant_id=restaurant_id)
        typer.echo(json.dumps(result.to_dict()))

    asyncio.run(_run())


@app.command(name="send-rfps")
def send_rfps_cmd(
    restaurant_id: int = typer.Argument(..., help="Restaurant ID"),
    limit: int = typer.Option(5, "--limit", help="Max distributors to email"),
    min_matches: int = typer.Option(2, "--min-matches", help="Min matched ingredient count"),
    deadline_days: int = typer.Option(5, "--deadline-days", help="Reply deadline window"),
) -> None:
    """Compose + send RFP emails for the top-N matched distributors."""

    async def _run() -> None:
        result = await send_rfps(
            restaurant_id=restaurant_id,
            distributor_limit=limit,
            min_matches=min_matches,
            deadline_days=deadline_days,
        )
        typer.echo(json.dumps(result.to_dict(), indent=2))

    asyncio.run(_run())


@app.command(name="poll-inbox")
def poll_inbox_cmd(
    rfp_request_id: int = typer.Argument(..., help="RFP request to poll for"),
    force: bool = typer.Option(
        False, "--force", help="Compute recommendation regardless of deadline / replies"
    ),
) -> None:
    """Run one IMAP poll cycle for an RFP; parse quotes, send follow-ups,
    optionally finalize."""

    async def _run() -> None:
        from app.db import SessionLocal
        from app.models.rfp import RfpRequest

        async with SessionLocal() as session:
            rfp = await session.get(RfpRequest, rfp_request_id)
            if rfp is None:
                typer.echo(json.dumps({"error": f"rfp_request {rfp_request_id} not found"}))
                raise typer.Exit(1)
            restaurant_id = rfp.restaurant_id
        result = await poll_and_process(
            restaurant_id=restaurant_id,
            rfp_request_id=rfp_request_id,
            force_recommendation=force,
        )
        typer.echo(json.dumps(result.to_dict(), indent=2, default=str))

    asyncio.run(_run())


@app.command(name="finalize")
def finalize_cmd(
    rfp_request_id: int = typer.Argument(..., help="RFP request to finalize"),
) -> None:
    """Force-compute the recommendation for an RFP regardless of deadline."""

    async def _run() -> None:
        rec = await compute_for_rfp(rfp_request_id, force=True)
        typer.echo(json.dumps(rec.to_dict(), indent=2, default=str))

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Phase 7 — demo seed orchestration
# ---------------------------------------------------------------------------

# Tables we wipe with --reset-data. Order is FK-safe via CASCADE on a single
# TRUNCATE statement: PostgreSQL handles the dependency chain when every
# affected table is listed. Distributors and restaurants are PRESERVED
# (seed data + idempotent restaurant row); the schema is NEVER touched.
_DEMO_DATA_TABLES = [
    # rfp + quote universe (root → leaves; CASCADE follows inbound FKs)
    "rfp_requests",
    "rfp_request_items",
    "rfp_emails",
    "imap_seen_uids",
    "quotes",
    "recommendations",
    # menu universe
    "dishes",
    "dish_ingredients",
    "ingredient_prices",
    "ingredients",
    # observability
    "llm_usage",
]


async def _truncate_demo_data() -> None:
    """Wipe demo content rows; preserve `distributors`, `restaurants`, and the schema.

    Single TRUNCATE ... CASCADE statement so PostgreSQL resolves all FK
    dependencies atomically — no half-completed truncation.
    """
    tables_csv = ", ".join(_DEMO_DATA_TABLES)
    async with SessionLocal() as session, session.begin():
        await session.execute(text(f"TRUNCATE TABLE {tables_csv} RESTART IDENTITY CASCADE"))


async def _restaurant_id_for_demo() -> int | None:
    """Find the Sweetgreen demo restaurant, if it exists."""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(Restaurant).where(Restaurant.name == SWEETGREEN["name"])
            )
        ).scalar_one_or_none()
        return row.id if row else None


async def _ensure_restaurant() -> int:
    """Create (or fetch) the Sweetgreen restaurant. Idempotent."""
    existing = await _restaurant_id_for_demo()
    if existing is not None:
        return existing
    async with SessionLocal() as session:
        r = Restaurant(**SWEETGREEN)
        session.add(r)
        await session.commit()
        await session.refresh(r)
        return r.id


async def _count(model_cls) -> int:
    async with SessionLocal() as session:
        return int(
            (
                await session.execute(select(func.count()).select_from(model_cls))
            ).scalar_one()
        )


async def _count_dishes_for(restaurant_id: int) -> int:
    from app.models.dish import Dish

    async with SessionLocal() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Dish)
                    .where(Dish.restaurant_id == restaurant_id)
                )
            ).scalar_one()
        )


async def _count_unenriched_ingredients_for(restaurant_id: int) -> int:
    from app.models.dish import Dish
    from app.models.dish_ingredient import DishIngredient

    async with SessionLocal() as session:
        return int(
            (
                await session.execute(
                    select(func.count(Ingredient.id.distinct()))
                    .select_from(Ingredient)
                    .join(DishIngredient, DishIngredient.ingredient_id == Ingredient.id)
                    .join(Dish, Dish.id == DishIngredient.dish_id)
                    .where(
                        Dish.restaurant_id == restaurant_id,
                        Ingredient.usda_fdc_id.is_(None),
                    )
                )
            ).scalar_one()
        )


async def _latest_rfp_for(restaurant_id: int) -> int | None:
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(func.max(RfpRequest.id)).where(
                    RfpRequest.restaurant_id == restaurant_id
                )
            )
        ).scalar_one_or_none()


async def _run_demo(
    *,
    menu_path: Path,
    reset_data: bool,
    yes: bool,
) -> dict:
    summary: dict = {
        "reset": False,
        "restaurant_id": None,
        "menu": None,
        "enrich": None,
        "discover": None,
        "send_rfps": None,
        "rfp_request_id": None,
        "messages": [],
    }

    if reset_data:
        if not yes:
            typer.echo(
                "About to TRUNCATE: "
                + ", ".join(_DEMO_DATA_TABLES)
                + "\n(Distributors and restaurants preserved; schema untouched.)"
            )
            confirm = typer.confirm("Continue?", default=False)
            if not confirm:
                typer.echo("Aborted.")
                raise typer.Exit(1)
        await _truncate_demo_data()
        summary["reset"] = True
        summary["messages"].append("reset_data: demo tables truncated")

    # ---- 1. Restaurant -------------------------------------------------
    restaurant_id = await _ensure_restaurant()
    summary["restaurant_id"] = restaurant_id

    # ---- 2. Parse menu -------------------------------------------------
    dish_count = await _count_dishes_for(restaurant_id)
    if dish_count >= 10:
        summary["menu"] = {"skipped": True, "reason": f"{dish_count} dishes already exist"}
        summary["messages"].append(f"parse: skipped ({dish_count} dishes)")
    else:
        resolved_menu = menu_path
        if not resolved_menu.is_absolute():
            resolved_menu = (Path.cwd() / menu_path).resolve()
        if not resolved_menu.exists():
            raise typer.BadParameter(f"menu file not found: {resolved_menu}")
        result = await parse_menu(restaurant_id=restaurant_id, menu_path=resolved_menu)
        summary["menu"] = result.to_dict()
        summary["messages"].append(
            f"parse: {result.dishes_inserted} dishes, {result.ingredients_inserted} ingredients"
        )

    # ---- 3. Enrich -----------------------------------------------------
    unenriched = await _count_unenriched_ingredients_for(restaurant_id)
    if unenriched == 0 and await _count_dishes_for(restaurant_id) > 0:
        summary["enrich"] = {"skipped": True, "reason": "all ingredients already have FDC ids"}
        summary["messages"].append("enrich: skipped (already done)")
    else:
        result = await enrich_restaurant(restaurant_id=restaurant_id)
        summary["enrich"] = result.to_dict()
        summary["messages"].append(
            f"enrich: matched={result.ingredients_matched} prices={result.prices_inserted}"
        )

    # ---- 4. Discover ---------------------------------------------------
    if await _count(Distributor) >= 8:
        summary["discover"] = {"skipped": True, "reason": "distributors already seeded"}
        summary["messages"].append("discover: skipped (already seeded)")
    else:
        result = await discover_distributors(restaurant_id=restaurant_id)
        summary["discover"] = result.to_dict()
        summary["messages"].append(
            f"discover: {result.total_distributors} distributors"
        )

    # ---- 5. Send RFPs --------------------------------------------------
    existing_rfp = await _latest_rfp_for(restaurant_id)
    if existing_rfp is not None:
        summary["send_rfps"] = {
            "skipped": True,
            "reason": f"rfp_request_id={existing_rfp} already exists",
        }
        summary["rfp_request_id"] = existing_rfp
        summary["messages"].append(
            f"send_rfps: skipped (rfp_request_id={existing_rfp} exists)"
        )
    else:
        result = await send_rfps(
            restaurant_id=restaurant_id,
            distributor_limit=5,
            min_matches=2,
            deadline_days=5,
        )
        summary["send_rfps"] = result.to_dict()
        summary["rfp_request_id"] = result.rfp_request_id
        summary["messages"].append(
            f"send_rfps: rfp_request_id={result.rfp_request_id}, "
            f"emails_sent={result.emails_sent}, emails_failed={result.emails_failed}"
        )

    return summary


@app.command(name="run-demo")
def run_demo_cmd(
    menu: Path = typer.Option(
        Path("../data/menus/sweetgreen.html"),
        "--menu",
        help="Path to menu HTML/text file (relative to backend/)",
    ),
    reset_data: bool = typer.Option(
        False,
        "--reset-data",
        help="TRUNCATE demo content rows first (preserves distributors + restaurants + schema)",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Skip the reset confirmation prompt"
    ),
) -> None:
    """End-to-end demo seed: restaurant → parse → enrich → discover → send_rfps.

    Idempotent by default — each step skips if its output already exists.
    Use --reset-data to clear demo content rows before running.
    """
    summary = asyncio.run(_run_demo(menu_path=menu, reset_data=reset_data, yes=yes))
    typer.echo("\n=== Demo seed summary ===")
    for msg in summary["messages"]:
        typer.echo(f"  ✓ {msg}")
    rfp_id = summary["rfp_request_id"]
    if rfp_id is not None:
        typer.echo(
            "\nDemo data ready.\n"
            f"  restaurant_id = {summary['restaurant_id']}\n"
            f"  rfp_request_id = {rfp_id}\n"
            "\nReply to the emails in your inbox, then run:\n"
            "  make poll       # parses replies, sends follow-ups if needed\n"
            "  make finalize   # forces recommendation regardless of completeness"
        )
    # Machine-readable footer for callers who pipe.
    typer.echo("\n" + json.dumps(summary, default=str))


@app.command(name="latest-rfp")
def latest_rfp_cmd() -> None:
    """Print the most recent rfp_request_id for the demo restaurant, or exit 1 if none."""

    async def _run() -> None:
        rid = await _restaurant_id_for_demo()
        if rid is None:
            sys.stderr.write("no demo restaurant in DB — run `make demo` first\n")
            raise typer.Exit(1)
        rfp_id = await _latest_rfp_for(rid)
        if rfp_id is None:
            sys.stderr.write(
                f"no RFPs for restaurant {rid} — run `make demo` to send some\n"
            )
            raise typer.Exit(1)
        typer.echo(str(rfp_id))

    asyncio.run(_run())


@app.command(name="poll-latest")
def poll_latest_cmd(
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Convenience wrapper: poll-inbox on the most recent RFP."""

    async def _run() -> None:
        rid = await _restaurant_id_for_demo()
        if rid is None:
            sys.stderr.write("no demo restaurant — run `make demo` first\n")
            raise typer.Exit(1)
        rfp_id = await _latest_rfp_for(rid)
        if rfp_id is None:
            sys.stderr.write("no RFPs in DB — run `make demo` first\n")
            raise typer.Exit(1)
        result = await poll_and_process(
            restaurant_id=rid,
            rfp_request_id=rfp_id,
            force_recommendation=force,
        )
        typer.echo(json.dumps(result.to_dict(), indent=2, default=str))

    asyncio.run(_run())


@app.command(name="finalize-latest")
def finalize_latest_cmd() -> None:
    """Convenience wrapper: finalize the most recent RFP."""

    async def _run() -> None:
        rid = await _restaurant_id_for_demo()
        if rid is None:
            sys.stderr.write("no demo restaurant — run `make demo` first\n")
            raise typer.Exit(1)
        rfp_id = await _latest_rfp_for(rid)
        if rfp_id is None:
            sys.stderr.write("no RFPs in DB — run `make demo` first\n")
            raise typer.Exit(1)
        rec = await compute_for_rfp(rfp_id, force=True)
        typer.echo(json.dumps(rec.to_dict(), indent=2, default=str))

    asyncio.run(_run())


if __name__ == "__main__":
    app()
