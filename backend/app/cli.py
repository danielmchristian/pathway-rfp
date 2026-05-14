"""Pathway RFP terminal CLI. Invoke with `python -m app.cli ...`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from sqlalchemy import select

from app.db import SessionLocal
from app.models.restaurant import Restaurant
from app.services.menu_parser import parse_menu

app = typer.Typer(add_completion=False, no_args_is_help=True)

SWEETGREEN = {
    "name": "Sweetgreen — Park Road Shopping Center",
    "address": "4329 Park Rd",
    "city": "Charlotte",
    "state": "NC",
    "zip": "28209",
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
    typer.echo(json.dumps({"restaurant_id": rid, **SWEETGREEN}))


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


if __name__ == "__main__":
    app()
