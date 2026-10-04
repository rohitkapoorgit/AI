"""BART (Bay Area Rapid Transit) API client — api.bart.gov, real-time departures + advisories.

Register a free key at https://api.bart.gov/api/register.aspx and set BART_API_KEY.
Falls back to BART's own publicly documented test key if unset — fine for development,
but it's shared/rate-limited, so get your own for real use.
"""

from __future__ import annotations

import os

import httpx

_BASE_URL = "https://api.bart.gov/api"
# `or` (not os.environ.get's default arg) so a present-but-empty BART_API_KEY — e.g. the
# blank line .env.example ships, meant to opt into this fallback — still falls back
# correctly. os.environ.get's default only applies when the key is entirely absent, not
# when it's set to "" (caused a real 400 from BART's API in testing).
_API_KEY = os.environ.get("BART_API_KEY") or "MW9S-E7SL-26DU-VV8V"

# Dublin/Pleasanton is a line terminus (Blue Line), so every departure from here heads
# toward SF, passing through Embarcadero and Montgomery further up the same line —
# no destination filtering needed.
ORIGIN_STATION = "DUBL"


def _cdata(value: object) -> str:
    """BART's "json" API is really XML converted to JSON, so text fields nest under this key."""
    if isinstance(value, dict):
        return value.get("#cdata-section", "")
    return str(value or "")


async def get_departures() -> dict:
    """Next real-time departures from Dublin/Pleasanton toward San Francisco."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_BASE_URL}/etd.aspx",
            params={"cmd": "etd", "orig": ORIGIN_STATION, "key": _API_KEY, "json": "y"},
        )
        resp.raise_for_status()
        payload = resp.json()

    stations = payload["root"].get("station") or []
    if not stations:
        return {"departures": []}

    departures = []
    for etd in stations[0].get("etd") or []:
        for estimate in etd.get("estimate") or []:
            departures.append(
                {
                    "destination": etd["destination"],
                    "minutes": estimate["minutes"],  # numeric string, or "Leaving"
                    "delay_seconds": int(estimate.get("delay") or 0),
                    "cancelled": estimate.get("cancelflag") == "1",
                }
            )
    return {"departures": departures[:6]}


async def get_service_advisories() -> dict:
    """Current system-wide BART service advisories (delays, disruptions, planned track work)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_BASE_URL}/bsa.aspx",
            params={"cmd": "bsa", "key": _API_KEY, "json": "y"},
        )
        resp.raise_for_status()
        payload = resp.json()

    raw = payload["root"].get("bsa") or []
    if isinstance(raw, dict):  # BART returns a bare object, not a list, when there's exactly one
        raw = [raw]

    return {
        "advisories": [
            {
                "type": item.get("type"),
                "station": item.get("station"),
                "description": _cdata(item.get("description")),
            }
            for item in raw
        ]
    }


GET_DEPARTURES_SCHEMA = {
    "name": "get_departures",
    "description": (
        "Get the next real-time train departures from Dublin/Pleasanton station toward "
        "San Francisco (this line passes through Embarcadero and Montgomery)."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

GET_ADVISORIES_SCHEMA = {
    "name": "get_service_advisories",
    "description": (
        "Get current system-wide BART service advisories (delays, disruptions, planned "
        "track work) — check whether any affect the Blue Line or the Dublin/Pleasanton, "
        "Embarcadero, or Montgomery stations."
    ),
    "input_schema": {"type": "object", "properties": {}},
}
