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


if __name__ == "__main__":
    app()
