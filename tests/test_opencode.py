import json

import httpx
import pytest

from sessiondeck.events import parse_sse
from sessiondeck.models import DeckError, Endpoint, PermissionRef
from sessiondeck.opencode import OpenCodeClient, normalize_endpoint


def client_for(handler):
    return OpenCodeClient(Endpoint("http://127.0.0.1:12345"), transport=httpx.MockTransport(handler))


def snapshot_handler(request):
    paths = {
        "/global/health": {"healthy": True, "version": "1.19.2"},
        "/session/ses_test": {"id": "ses_test", "directory": "/tmp/a b/проект", "title": "Test", "time": {"updated": 1000}},
        "/session/status": {}, "/permission": [], "/question": [],
    }
    return httpx.Response(200, json=paths[request.url.path])


async def test_idle_omitted_after_successful_snapshot():
    async with client_for(snapshot_handler) as client:
        assert (await client.snapshot("/tmp/a b/проект", "ses_test")).execution == "idle"


async def test_failed_snapshot_is_not_idle():
    async with client_for(lambda r: httpx.Response(503)) as client:
        with pytest.raises(DeckError):
            await client.snapshot("/tmp", "ses_test")


async def test_directory_is_encoded():
    def handler(request):
        if request.url.path != "/global/health":
            assert request.url.params["directory"] == "/tmp/a b/проект"
        return snapshot_handler(request)
    async with client_for(handler) as client:
        await client.snapshot("/tmp/a b/проект", "ses_test")


@pytest.mark.parametrize("decision", ["once", "reject"])
async def test_reply_is_once_or_reject(decision):
    def handler(request):
        assert json.loads(request.content) == {"reply": decision}
        assert request.url.path == "/permission/per_test/reply"
        return httpx.Response(200, json=True)
    async with client_for(handler) as client:
        ref = PermissionRef(client.endpoint, "/tmp", "ses_test", "per_test")
        assert await client.reply(ref, decision)
        with pytest.raises(DeckError):
            await client.reply(ref, "always")


async def test_sse_comments_multiline_and_bad_frames():
    async def lines():
        for line in [": ping", "data: {", 'data: "type":"question.asked",', 'data: "properties":{"text":"вопрос"}}', "", "data: invalid", "", 'data: {"type":"future"}', ""]:
            yield line
    assert [e["type"] async for e in parse_sse(lines())] == ["question.asked", "future"]


@pytest.mark.parametrize("url", ["https://127.0.0.1:4", "http://example.com:4", "http://u:p@localhost:4", "http://localhost", "http://localhost:4/secret"])
def test_invalid_endpoint(url):
    with pytest.raises(DeckError):
        normalize_endpoint(url)


async def test_wrong_version_and_invalid_json():
    for response in [httpx.Response(200, json={"healthy": True, "version": "2.0"}), httpx.Response(200, text="not json")]:
        async with client_for(lambda r: response) as client:
            with pytest.raises(DeckError):
                await client.health()
