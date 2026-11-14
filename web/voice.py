"""Voice in the supplier portal (ElevenLabs Agents). Mounted by web/app.py.

The supplier presses "Talk" in the console and speaks instead of typing. The
ElevenLabs agent is only ears and a mouth:

  * anything about the recall itself (report a problem, approve, pause, "how is
    it going?") goes to the console's own chat, POST /chat (agent/intake.py), so
    speaking and typing pass through exactly the same gate;
  * questions about one unit, order, shipment or customer use the read-only
    lookups below, which ask the warehouse, order and carrier systems live.

Both run as *client tools*: functions in the browser (web/voice.js). ElevenLabs
never gets a route into this server, and the API key never reaches the browser:
the page asks for a short-lived signed URL and opens the conversation with that.

There is also a narrator (POST /voice/speak): with "Narrate" switched on, the console
reads Tracer's updates aloud as they appear: what happened to the product and what
the supplier should do next. It needs only the API key, not the agent.

Setup: ELEVENLABS_API_KEY in .env, then `python -m scripts.setup_voice_agent`
once, then put the printed ELEVENLABS_AGENT_ID in .env.
"""
import os
import re

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel


ELEVENLABS_BASE = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")
# Narrator: a stock ElevenLabs voice ("George") and the low-latency model, so a spoken
# update starts within a second or so of the event. Both can be changed in .env.
NARRATOR_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
NARRATOR_MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
MAX_SPOKEN_CHARS = 700   # one update, not an essay: bounds latency and ElevenLabs credits
MOCKS = {
    "warehouse": os.getenv("WAREHOUSE_URL", "http://localhost:8001"),
    "orders": os.getenv("ORDERS_URL", "http://localhost:8002"),
    "carrier": os.getenv("CARRIER_URL", "http://localhost:8003"),
}
# What a lookup kind reads, and how the IDs look (prefix, digits) so "trk 5003" or
# "unit 7" from speech-to-text still finds TRK-5003 / UNIT-0007.
LOOKUPS = {
    "unit": ("warehouse", "/inventory/{id}", "UNIT", 4),
    "order": ("orders", "/orders/{id}", "ORD", 4),
    "shipment": ("carrier", "/shipments/{id}", "TRK", 4),
    "notices": ("carrier", "/notifications", "CUST", 3),
}
_ID = re.compile(r"^([A-Z]+)[\s\-_]*0*(\d+)$")

router = APIRouter(prefix="/voice")


def normalize_id(raw: str, prefix: str, digits: int) -> str:
    """'trk 5003' → 'TRK-5003', 'unit 7' → 'UNIT-0007', '5003' → 'TRK-5003'."""
    text = raw.strip().upper()
    if text.isdigit():
        text = f"{prefix}-{text}"
    m = _ID.match(text)
    if not m:
        return text
    return f"{m.group(1)}-{m.group(2).zfill(digits)}"


def _settings() -> tuple[str | None, str | None]:
    return os.getenv("ELEVENLABS_API_KEY") or None, os.getenv("ELEVENLABS_AGENT_ID") or None


@router.get("/config")
def config():
    """Lets the page show the talk button, or say what's missing."""
    key, agent = _settings()
    missing = [name for name, val in (("ELEVENLABS_API_KEY", key), ("ELEVENLABS_AGENT_ID", agent))
               if not val]
    return {"enabled": not missing, "missing": missing, "narrator": bool(key)}


class SpeakIn(BaseModel):
    text: str


@router.post("/speak")
def speak(req: SpeakIn):
    """Narrator: one of Tracer's updates as speech (MP3). Needs only ELEVENLABS_API_KEY."""
    key, _ = _settings()
    if not key:
        raise HTTPException(503, "narrator not configured: set ELEVENLABS_API_KEY")
    text = " ".join(req.text.split())[:MAX_SPOKEN_CHARS]
    if not text:
        raise HTTPException(422, "nothing to say")
    try:
        r = httpx.post(f"{ELEVENLABS_BASE}/v1/text-to-speech/{NARRATOR_VOICE}",
                       headers={"xi-api-key": key}, json={"text": text, "model_id": NARRATOR_MODEL},
                       timeout=20)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"can't reach ElevenLabs: {exc.__class__.__name__}") from None
    if r.status_code != 200:
        raise HTTPException(502, f"ElevenLabs returned {r.status_code}: {r.text[:200]}")
    return Response(r.content, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@router.get("/signed-url")
def signed_url():
    """Short-lived URL for one conversation with our agent. The API key stays here."""
    key, agent = _settings()
    if not key or not agent:
        raise HTTPException(503, "voice not configured: set ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID")
    try:
        r = httpx.get(f"{ELEVENLABS_BASE}/v1/convai/conversation/get-signed-url",
                      params={"agent_id": agent}, headers={"xi-api-key": key}, timeout=10)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"can't reach ElevenLabs: {exc.__class__.__name__}") from None
    if r.status_code != 200:
        # Never echo the key; ElevenLabs' error body is safe to show.
        raise HTTPException(502, f"ElevenLabs returned {r.status_code}: {r.text[:200]}")
    return {"signed_url": r.json()["signed_url"]}


@router.get("/lookup/{kind}/{record_id}")
def lookup(kind: str, record_id: str, recall_id: str | None = None):
    """One record, read live from the system that owns it (not from our own database)."""
    if kind not in LOOKUPS:
        raise HTTPException(404, f"unknown lookup {kind!r}; use one of {sorted(LOOKUPS)}")
    system, path, prefix, digits = LOOKUPS[kind]
    rid = normalize_id(record_id, prefix, digits)
    params = {}
    if kind == "notices":
        params = {"customer_id": rid, **({"recall_id": recall_id} if recall_id else {})}
    try:
        r = httpx.get(MOCKS[system] + path.format(id=rid), params=params, timeout=3)
    except httpx.HTTPError:
        raise HTTPException(502, f"the {system} system is not reachable") from None
    if r.status_code == 404:
        return {"found": False, "id": rid, "system": system}
    r.raise_for_status()
    data = r.json()
    if kind == "notices":
        return {"found": True, "customer_id": rid, "system": system, "notice_count": len(data),
                "notices": [{k: n.get(k) for k in ("recall_id", "channel", "sent_at")} for n in data]}
    return {"found": True, "id": rid, "system": system, "record": data}
