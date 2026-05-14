"""Pathway RFP terminal CLI. Invoke with `python -m app.cli ...`."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import typer
from sqlalchemy import select

from app.db import SessionLocal
from app.models.restaurant import Restaurant
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


if __name__ == "__main__":
    app()
