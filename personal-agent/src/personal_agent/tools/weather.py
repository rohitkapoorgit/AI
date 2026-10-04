"""National Weather Service (api.weather.gov) client — free, no API key required.

NWS asks API consumers to identify themselves via User-Agent (app name + contact,
for abuse/rate-limit follow-up) rather than an API key — configure your own contact
via the WEATHER_API_CONTACT env var, don't hardcode one.

Locations are looked up by lat/lon rather than city name, since "Dublin" alone is
ambiguous (Dublin, CA vs. Dublin, Ireland).
"""

from __future__ import annotations

import os

import httpx

LOCATIONS = {
    "san_francisco": {"label": "San Francisco, CA", "lat": 37.7749, "lon": -122.4194},
    "dublin_ca": {"label": "Dublin, CA", "lat": 37.7022, "lon": -121.9358},
}

_BASE_URL = "https://api.weather.gov"
_CONTACT = os.environ.get("WEATHER_API_CONTACT", "personal-agent-project (contact-not-set)")
_HEADERS = {
    "User-Agent": f"personal-morning-digest/0.1 ({_CONTACT})",
    "Accept": "application/geo+json",
}


class UnknownLocationError(ValueError):
    pass


async def get_forecast(location: str) -> dict:
    """Return today's/tonight's forecast for a known location key.

    NWS forecasts are two-step: /points/{lat},{lon} resolves to a forecast office
    and grid, whose URL then serves the actual forecast periods.
    """
    if location not in LOCATIONS:
        raise UnknownLocationError(f"Unknown location {location!r}; known: {list(LOCATIONS)}")

    loc = LOCATIONS[location]
    async with httpx.AsyncClient(headers=_HEADERS, timeout=15.0) as client:
        points_resp = await client.get(f"{_BASE_URL}/points/{loc['lat']},{loc['lon']}")
        points_resp.raise_for_status()
        forecast_url = points_resp.json()["properties"]["forecast"]

        forecast_resp = await client.get(forecast_url)
        forecast_resp.raise_for_status()
        periods = forecast_resp.json()["properties"]["periods"]

    return {
        "location": loc["label"],
        "periods": [
            {
                "name": p["name"],
                "temperature": p["temperature"],
                "temperature_unit": p["temperatureUnit"],
                "short_forecast": p["shortForecast"],
                "precipitation_chance": (p.get("probabilityOfPrecipitation") or {}).get("value"),
                "wind": f"{p['windSpeed']} {p['windDirection']}",
            }
            # Today + tonight is enough context for a morning digest.
            for p in periods[:2]
        ],
    }


TOOL_SCHEMA = {
    "name": "get_forecast",
    "description": (
        "Get the weather forecast (today + tonight) for a known location. "
        f"Valid location values: {list(LOCATIONS)}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "enum": list(LOCATIONS),
                "description": "Which location to fetch the forecast for.",
            }
        },
        "required": ["location"],
    },
}
