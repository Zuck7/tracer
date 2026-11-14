"""Voice assistant: server routes (web/voice.py) and the ElevenLabs setup script.

No network: ElevenLabs is an httpx.MockTransport, the mocks are faked responses,
and Atlas is never touched.
"""
import json
import os

import httpx
import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:1")  # never connected in these tests

from fastapi.testclient import TestClient  # noqa: E402

from scripts import setup_voice_agent as setup_script  # noqa: E402
from web import app as web_app, voice  # noqa: E402

client = TestClient(web_app.app)


def fake_get(routes):
    """httpx.get stand-in: URL substring → (status, json)."""
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        for part, (status, body) in routes.items():
            if part in url:
                return httpx.Response(status, json=body, request=httpx.Request("GET", url))
        raise httpx.ConnectError("unreachable", request=httpx.Request("GET", url))

    get.calls = calls
    return get


@pytest.mark.parametrize("raw,prefix,digits,want", [
    ("TRK-5003", "TRK", 4, "TRK-5003"), ("trk 5003", "TRK", 4, "TRK-5003"),
    ("unit 7", "UNIT", 4, "UNIT-0007"), ("UNIT0012", "UNIT", 4, "UNIT-0012"),
    ("5003", "TRK", 4, "TRK-5003"), ("cust-118", "CUST", 3, "CUST-118"),
    ("ord_1001", "ORD", 4, "ORD-1001"),
])
def test_normalize_id(raw, prefix, digits, want):
    assert voice.normalize_id(raw, prefix, digits) == want


def test_config_reports_missing_settings(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_x")
    assert client.get("/voice/config").json() == {"enabled": False, "missing": ["ELEVENLABS_API_KEY"],
                                                  "narrator": False}


def test_signed_url_keeps_key_on_server(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_secret")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_x")
    get = fake_get({"get-signed-url": (200, {"signed_url": "wss://signed"})})
    monkeypatch.setattr(voice.httpx, "get", get)
    r = client.get("/voice/signed-url")
    assert r.json() == {"signed_url": "wss://signed"}
    assert "sk_secret" not in r.text
    assert get.calls[0]["headers"]["xi-api-key"] == "sk_secret"
    assert get.calls[0]["params"] == {"agent_id": "agent_x"}


def test_signed_url_errors_are_clear(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert client.get("/voice/signed-url").status_code == 503
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_secret")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent_x")
    monkeypatch.setattr(voice.httpx, "get", fake_get({"get-signed-url": (401, {"detail": "bad key"})}))
    r = client.get("/voice/signed-url")
    assert r.status_code == 502 and "401" in r.json()["detail"] and "sk_secret" not in r.text


def test_narrator_speaks_one_bounded_update(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_secret")
    monkeypatch.delenv("ELEVENLABS_AGENT_ID", raising=False)   # the narrator needs only the key
    sent = {}

    def post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, json=json)
        return httpx.Response(200, content=b"ID3-mp3-bytes", request=httpx.Request("POST", url))

    monkeypatch.setattr(voice.httpx, "post", post)
    r = client.post("/voice/speak", json={"text": "  I stopped   unexpectedly.\nPress Restart. " + "x" * 2000})
    assert r.status_code == 200 and r.content == b"ID3-mp3-bytes"
    assert r.headers["content-type"] == "audio/mpeg"
    assert sent["url"].endswith(f"/v1/text-to-speech/{voice.NARRATOR_VOICE}")
    assert sent["headers"] == {"xi-api-key": "sk_secret"}
    assert sent["json"]["text"].startswith("I stopped unexpectedly. Press Restart.")
    assert len(sent["json"]["text"]) == voice.MAX_SPOKEN_CHARS
    assert client.get("/voice/config").json()["narrator"] is True


def test_narrator_errors_are_clear(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert client.post("/voice/speak", json={"text": "hello"}).status_code == 503
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_secret")
    assert client.post("/voice/speak", json={"text": "   "}).status_code == 422
    monkeypatch.setattr(voice.httpx, "post", lambda url, **k: httpx.Response(
        401, json={"detail": "quota"}, request=httpx.Request("POST", url)))
    r = client.post("/voice/speak", json={"text": "hello"})
    assert r.status_code == 502 and "sk_secret" not in r.text


def test_lookup_reads_the_owning_system(monkeypatch):
    get = fake_get({
        "/shipments/TRK-5003": (200, {"tracking_no": "TRK-5003", "status": "in_transit"}),
        "/inventory/UNIT-0099": (404, {"detail": "not found"}),
        "/notifications": (200, [{"recall_id": "RC-GB-2611", "channel": "email", "sent_at": "t",
                                  "message": "long text the agent doesn't need"}]),
    })
    monkeypatch.setattr(voice.httpx, "get", get)
    ship = client.get("/voice/lookup/shipment/trk 5003").json()
    assert ship["found"] and ship["record"]["status"] == "in_transit"
    assert get.calls[-1]["url"] == "http://localhost:8003/shipments/TRK-5003"
    assert client.get("/voice/lookup/unit/UNIT-0099").json()["found"] is False
    notices = client.get("/voice/lookup/notices/cust-118?recall_id=RC-GB-2611").json()
    assert notices["notice_count"] == 1 and "message" not in notices["notices"][0]
    assert get.calls[-1]["params"] == {"customer_id": "CUST-118", "recall_id": "RC-GB-2611"}
    assert client.get("/voice/lookup/pallet/P1").status_code == 404


def test_lookup_when_system_is_down(monkeypatch):
    monkeypatch.setattr(voice.httpx, "get", fake_get({}))
    r = client.get("/voice/lookup/order/ORD-1001")
    assert r.status_code == 502 and "orders" in r.json()["detail"]


def test_console_has_the_talk_button():
    page = client.get("/console").text
    assert 'id="vbtn"' in page and 'id="vstatus"' in page and 'src="/voice.js"' in page
    assert 'id="nbtn"' in page
    js = client.get("/voice.js")
    assert js.headers["content-type"].startswith("text/javascript")
    for name, *_ in setup_script.TOOLS:  # every tool ElevenLabs knows has a browser handler
        assert f"{name}:" in js.text
    assert "await send(text)" in js.text  # speech goes through the console's own chat


# ---------- scripts/setup_voice_agent.py against a fake ElevenLabs ----------

def fake_elevenlabs(existing=()):
    log = []

    def handler(request: httpx.Request):
        body = json.loads(request.content) if request.content else None
        log.append((request.method, request.url.path, body, request.headers.get("xi-api-key")))
        path = request.url.path
        if request.method == "GET" and path == "/v1/convai/tools":
            return httpx.Response(200, json={"tools": [
                {"id": f"tool_old_{n}", "tool_config": {"name": n}} for n in existing]})
        if request.method == "POST" and path == "/v1/convai/tools":
            return httpx.Response(200, json={"id": f"tool_{body['tool_config']['name']}"})
        if request.method == "POST" and path == "/v1/convai/agents/create":
            return httpx.Response(200, json={"agent_id": "agent_new"})
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    api = setup_script.ElevenLabs("sk_test", httpx.Client(base_url="https://el.test",
                                                          transport=httpx.MockTransport(handler)))
    return api, log


def test_setup_creates_client_tools_then_agent():
    api, log = fake_elevenlabs()
    assert setup_script.setup(api, None) == "agent_new"
    tools = [b["tool_config"] for m, p, b, _ in log if (m, p) == ("POST", "/v1/convai/tools")]
    assert [t["name"] for t in tools] == [t[0] for t in setup_script.TOOLS]
    assert all(t["type"] == "client" and t["expects_response"] for t in tools)
    assert all(t["parameters"]["type"] == "object" for t in tools)
    assert tools[0]["name"] == "ask_tracer" and tools[0]["response_timeout_secs"] == 45
    agent = next(b for m, p, b, _ in log if p == "/v1/convai/agents/create")
    assert agent["conversation_config"]["agent"]["prompt"]["tool_ids"] == [f"tool_{t['name']}" for t in tools]
    assert all(key == "sk_test" for *_, key in log)


def test_setup_rerun_updates_instead_of_duplicating():
    api, log = fake_elevenlabs(existing=["lookup_shipment"])
    assert setup_script.setup(api, "agent_existing") == "agent_existing"
    assert ("PATCH", "/v1/convai/tools/tool_old_lookup_shipment") in [(m, p) for m, p, *_ in log]
    assert not any(p == "/v1/convai/agents/create" for _, p, *_ in log)
    assert ("PATCH", "/v1/convai/agents/agent_existing") in [(m, p) for m, p, *_ in log]
