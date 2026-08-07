"""Thin async client for the public NPPES NPI Registry API — no auth, no API key.

https://npiregistry.cms.hhs.gov/api/ — confirmed live (2026-08): `version=2.1` is required (the
only currently-accepted value); successful responses are `{"result_count": int, "results": [...]}`;
failures are `{"Errors": [{"description": ..., "field": ..., "number": ...}]}`, not an HTTP error
status. No language-spoken field exists anywhere in the response schema — confirmed by inspecting
real response bodies; there's nowhere for `Doctor.languages` to come from here.

Deliberately thin and unopinionated — just translates Python kwargs into NPPES query params and
NPPES's own error shape into `NppesApiError`. Domain-level decisions (which fields to search on,
how to parse a name into first/last, restricting to individuals) live in sandbox_backend.py, not
here, so this client stays reusable if anything else ever needs raw NPPES access.
"""

import os

import httpx

_DEFAULT_BASE_URL = "https://npiregistry.cms.hhs.gov/api"
_API_VERSION = "2.1"


class NppesApiError(Exception):
    """Raised for NPPES transport failures or an {"Errors": [...]} response body."""


class NppesClient:
    def __init__(self, *, base_url: str | None = None, client: httpx.AsyncClient | None = None):
        self._base_url = (base_url or os.environ.get("NPPES_API_BASE_URL", _DEFAULT_BASE_URL)).rstrip(
            "/"
        )
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None

    async def search(self, **params: str | int | None) -> list[dict]:
        """params are passed straight through as NPPES query params (e.g. taxonomy_description=,
        city=, state=, first_name=, last_name=, organization_name=, postal_code=, number=, limit=,
        skip=, enumeration_type=). None values are dropped. `version` is always forced to 2.1."""
        query = {k: v for k, v in params.items() if v is not None}
        query["version"] = _API_VERSION
        body = await self._get(query)
        return body.get("results", [])

    async def get_by_number(self, npi: str) -> dict | None:
        """Exact NPI lookup — confirmed live that `number=` works for this."""
        results = await self.search(number=npi)
        return results[0] if results else None

    async def _get(self, params: dict) -> dict:
        try:
            response = await self._client.get(f"{self._base_url}/", params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NppesApiError(f"NPPES request failed: {exc}") from exc

        body = response.json()
        if "Errors" in body:
            raise NppesApiError(f"NPPES error: {body['Errors']}")
        return body

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
