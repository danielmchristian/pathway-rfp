"""Verify the placeholder commodity slugs + Atlanta report IDs against the live
USDA AMS Market News (MARS) API.

Usage:
    USDA_AMS_API_KEY=... uv run python scripts/verify_ams_map.py

What it does:
  1. Hits GET /services/v1.2/commodities and prints commodity names whose names
     match (case-insensitive substring) any of our target ingredients.
  2. Hits GET /services/v1.2/reports and lists Atlanta-area report IDs for
     vegetables and fruits with their slug_ids.
  3. Writes the output to scripts/verify_ams_map.out.txt for manual review.
  4. Suggests edits to app/services/commodity_map.py.

This is exploratory — it doesn't auto-edit the map. Treat the output as
ground truth and update commodity_map.py by hand.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

TARGETS = [
    "kale",
    "romaine",
    "spinach",
    "tomato",
    "cucumber",
    "onion",
    "avocado",
    "broccoli",
    "sweet potato",
    "carrot",
    "cabbage",
    "bell pepper",
    "cilantro",
    "lemon",
    "lime",
]

MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"
OUT_PATH = Path(__file__).resolve().parent / "verify_ams_map.out.txt"


async def fetch_commodities(client: httpx.AsyncClient, key: str) -> list[dict]:
    r = await client.get(f"{MARS_BASE}/commodities", auth=(key, ""), timeout=20.0)
    r.raise_for_status()
    payload = r.json()
    return payload.get("results", payload) if isinstance(payload, dict) else payload


async def fetch_reports(client: httpx.AsyncClient, key: str) -> list[dict]:
    r = await client.get(f"{MARS_BASE}/reports", auth=(key, ""), timeout=20.0)
    r.raise_for_status()
    payload = r.json()
    return payload.get("results", payload) if isinstance(payload, dict) else payload


def match_targets(commodities: list[dict]) -> dict[str, list[str]]:
    """Map each target ingredient → list of commodity names that contain its tokens."""
    out: dict[str, list[str]] = {t: [] for t in TARGETS}
    for c in commodities:
        name = (c.get("commodity_name") or c.get("name") or "").strip()
        if not name:
            continue
        lower = name.lower()
        for target in TARGETS:
            tokens = target.split()
            if all(tok in lower for tok in tokens):
                out[target].append(name)
    return out


def filter_atlanta_reports(reports: list[dict]) -> list[dict]:
    return [
        r
        for r in reports
        if "atlanta" in str(r.get("market_location_name") or r.get("office_name") or "").lower()
    ]


async def main() -> int:
    key = os.environ.get("USDA_AMS_API_KEY", "").strip()
    if not key:
        print(
            "ERROR: USDA_AMS_API_KEY not set in environment.\n"
            "Sign up free at https://mymarketnews.ams.usda.gov/ and re-run.",
            file=sys.stderr,
        )
        return 2

    lines: list[str] = []

    async with httpx.AsyncClient() as client:
        try:
            commodities = await fetch_commodities(client, key)
            reports = await fetch_reports(client, key)
        except httpx.HTTPStatusError as exc:
            print(f"API error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
            return 1
        except httpx.HTTPError as exc:
            print(f"HTTP error: {exc}", file=sys.stderr)
            return 1

    lines.append(f"# Verification run — {len(commodities)} commodities, {len(reports)} reports\n")

    lines.append("## Commodity matches for our target ingredients\n")
    matches = match_targets(commodities)
    for target, found in matches.items():
        if found:
            lines.append(f"- **{target}** → {found[:5]}")
        else:
            lines.append(f"- **{target}** → (no commodities matched — pricing_unavailable)")

    lines.append("\n## Atlanta-area reports\n")
    atlanta = filter_atlanta_reports(reports)
    for r in atlanta:
        lines.append(
            f"- slug_id={r.get('slug_id') or r.get('id')}  "
            f"name={r.get('report_title') or r.get('name')}  "
            f"market={r.get('market_location_name') or r.get('office_name')}  "
            f"section={r.get('section') or r.get('commodity_category')}"
        )

    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_PATH}\n")
    print("\n".join(lines))

    print(
        "\nNext step: update app/services/commodity_map.py with the verified "
        "slug strings and report IDs above."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
