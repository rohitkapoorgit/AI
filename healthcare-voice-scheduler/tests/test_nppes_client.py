import httpx
import pytest

from voice_agent.scheduling.nppes_client import NppesApiError, NppesClient

_SAMPLE_RESULT = {
    "number": "1962279315",
    "enumeration_type": "NPI-1",
    "basic": {"first_name": "THOMAS", "last_name": "BERNIER", "status": "A"},
    "taxonomies": [{"code": "1835C0206X", "desc": "Pharmacist, Cardiology", "primary": True}],
    "addresses": [
        {"city": "CHICAGO", "state": "IL", "address_purpose": "LOCATION", "postal_code": "606123841"}
    ],
}


def _client_with(handler) -> NppesClient:
    transport = httpx.MockTransport(handler)
    return NppesClient(client=httpx.AsyncClient(transport=transport))


@pytest.mark.asyncio
async def test_search_always_sends_version_and_forwards_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"result_count": 0, "results": []})

    client = _client_with(handler)
    await client.search(taxonomy_description="Cardiology", city="Chicago", state="IL")

    assert captured["version"] == "2.1"
    assert captured["taxonomy_description"] == "Cardiology"
    assert captured["city"] == "Chicago"
    assert captured["state"] == "IL"


@pytest.mark.asyncio
async def test_search_drops_none_valued_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"result_count": 0, "results": []})

    client = _client_with(handler)
    await client.search(city="Austin", state=None, name=None)

    assert "state" not in captured
    assert "name" not in captured
    assert captured["city"] == "Austin"


@pytest.mark.asyncio
async def test_search_returns_results_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result_count": 1, "results": [_SAMPLE_RESULT]})

    client = _client_with(handler)
    results = await client.search(taxonomy_description="Cardiology")

    assert results == [_SAMPLE_RESULT]


@pytest.mark.asyncio
async def test_search_raises_on_errors_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Errors": [{"description": "No valid search criteria provided", "field": "generic"}]},
        )

    client = _client_with(handler)
    with pytest.raises(NppesApiError, match="No valid search criteria"):
        await client.search()


@pytest.mark.asyncio
async def test_search_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = _client_with(handler)
    with pytest.raises(NppesApiError):
        await client.search(city="Austin")


@pytest.mark.asyncio
async def test_get_by_number_returns_first_result():
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params)["number"] == "1962279315"
        return httpx.Response(200, json={"result_count": 1, "results": [_SAMPLE_RESULT]})

    client = _client_with(handler)
    result = await client.get_by_number("1962279315")

    assert result == _SAMPLE_RESULT


@pytest.mark.asyncio
async def test_get_by_number_returns_none_when_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result_count": 0, "results": []})

    client = _client_with(handler)
    result = await client.get_by_number("0000000000")

    assert result is None
