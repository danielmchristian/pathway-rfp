"""Google Places (New API) integration for distributor discovery.

Only invoked when `GOOGLE_PLACES_API_KEY` is set. Returns normalized
PlaceCandidate records; the orchestrator handles dedup against seed and
the Claude-driven noise filter.

API choice: the *new* Places API (`places.googleapis.com/v1/places:searchNearby`).
It returns the fields we need inline via a FieldMask — no separate Place
Details call required — and it's the path Google directs new keys to.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.config import settings
from app.llm import MODEL_ID
from app.llm.client import get_client
from app.llm.tools import CLASSIFY_DISTRIBUTORS
from app.llm.usage import traced_call
from app.utils.http_retry import request_with_retry

log = structlog.get_logger("google_places")

PLACES_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.types",
        "places.primaryType",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
    ]
)

# Two structured queries per discovery run (per plan):
#   1. Structured types — Google's own taxonomy for food-distribution-ish places.
#   2. Free-text-via-types — wider net, more recall.
PLACES_QUERIES: list[dict[str, Any]] = [
    {"includedTypes": ["food_store", "wholesaler"], "maxResultCount": 20},
    {"includedTypes": ["grocery_store", "supermarket"], "maxResultCount": 20},
]


@dataclass
class PlaceCandidate:
    place_id: str
    name: str
    address: str | None
    latitude: float | None
    longitude: float | None
    phone: str | None
    website: str | None
    types: list[str]


def _parse_place(p: dict[str, Any]) -> PlaceCandidate | None:
    pid = p.get("id")
    name_obj = p.get("displayName") or {}
    name = name_obj.get("text") if isinstance(name_obj, dict) else None
    if not pid or not name:
        return None
    loc = p.get("location") or {}
    return PlaceCandidate(
        place_id=str(pid),
        name=name,
        address=p.get("formattedAddress"),
        latitude=loc.get("latitude"),
        longitude=loc.get("longitude"),
        phone=p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber"),
        website=p.get("websiteUri"),
        types=list(p.get("types") or []),
    )


async def _query_nearby(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    latitude: float,
    longitude: float,
    radius_m: float,
    query: dict[str, Any],
) -> list[PlaceCandidate]:
    body = {
        "locationRestriction": {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": radius_m,
            }
        },
        **query,
    }
    response = await request_with_retry(
        client,
        "POST",
        PLACES_URL,
        json=body,
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": PLACES_FIELD_MASK,
            "Content-Type": "application/json",
        },
        timeout=15.0,
        label=f"places.searchNearby[{query.get('includedTypes')}]",
    )
    if response is None or response.status_code >= 400:
        log.warning(
            "places.query.failed",
            status=response.status_code if response else None,
            body=response.text[:300] if response else None,
        )
        return []
    payload = response.json()
    places = payload.get("places") or []
    out: list[PlaceCandidate] = []
    seen_ids: set[str] = set()
    for p in places:
        parsed = _parse_place(p)
        if parsed and parsed.place_id not in seen_ids:
            seen_ids.add(parsed.place_id)
            out.append(parsed)
    return out


async def search_distributor_candidates(
    client: httpx.AsyncClient,
    *,
    latitude: float,
    longitude: float,
    radius_m: float = 50_000.0,
) -> list[PlaceCandidate]:
    """Run all configured Places queries and dedupe by place_id."""
    if not settings.google_places_api_key:
        return []
    api_key = settings.google_places_api_key
    all_candidates: dict[str, PlaceCandidate] = {}
    for query in PLACES_QUERIES:
        batch = await _query_nearby(
            client,
            api_key=api_key,
            latitude=latitude,
            longitude=longitude,
            radius_m=radius_m,
            query=query,
        )
        for c in batch:
            all_candidates.setdefault(c.place_id, c)
    return list(all_candidates.values())


@dataclass
class FilteredCandidates:
    kept: list[PlaceCandidate]
    rejected: list[tuple[PlaceCandidate, str]]  # (candidate, reason)


async def filter_noise_with_claude(
    candidates: list[PlaceCandidate],
) -> FilteredCandidates:
    """Ask Claude to drop retail/non-distributor entries.

    Logs to llm_usage stage='distributor_filter'. Returns the original list
    unchanged on any failure (better to keep noise than to drop everything).
    """
    if not candidates:
        return FilteredCandidates(kept=[], rejected=[])

    payload = [
        {
            "index": i,
            "name": c.name,
            "address": c.address,
            "types": c.types,
        }
        for i, c in enumerate(candidates)
    ]

    client = get_client()
    try:
        async with traced_call("distributor_filter") as call:
            response = await client.messages.create(
                model=MODEL_ID,
                max_tokens=2048,
                tools=[CLASSIFY_DISTRIBUTORS],
                tool_choice={"type": "tool", "name": "classify_distributor_candidates"},
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Filter these Google Places results to genuine wholesale food "
                            "distributors a restaurant would RFP for ingredients. "
                            "Reject retail chains, warehouse clubs, individual restaurants, "
                            "and non-food businesses.\n\n"
                            "Candidates:\n" + json.dumps(payload, indent=2)
                        ),
                    }
                ],
            )
            call.bind(response)
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("places.filter.exception", error=str(exc))
        return FilteredCandidates(kept=list(candidates), rejected=[])

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None or not isinstance(tool_use.input, dict):
        log.warning("places.filter.no_tool_use")
        return FilteredCandidates(kept=list(candidates), rejected=[])

    decisions = tool_use.input.get("decisions") or []
    decision_by_index: dict[int, tuple[bool, str]] = {}
    for d in decisions:
        try:
            decision_by_index[int(d["index"])] = (
                bool(d.get("is_wholesale_distributor", False)),
                str(d.get("reason", "")),
            )
        except (KeyError, TypeError, ValueError):
            continue

    kept: list[PlaceCandidate] = []
    rejected: list[tuple[PlaceCandidate, str]] = []
    for i, candidate in enumerate(candidates):
        keep, reason = decision_by_index.get(i, (True, "no decision returned"))
        if keep:
            kept.append(candidate)
        else:
            rejected.append((candidate, reason))
    log.info(
        "places.filter.summary",
        total=len(candidates),
        kept=len(kept),
        rejected=len(rejected),
    )
    return FilteredCandidates(kept=kept, rejected=rejected)
