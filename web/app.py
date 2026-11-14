"""Live log for the demo, port 8000.   uvicorn web.app:app --port 8000

This is a window onto the agent, not the product. It shows:
  - every audit event as it happens, pushed by a MongoDB change stream (no polling)
  - verified/total per action, the countdown to the next truck pickup
  - buttons a judge can press: drop writes, change API, kill agent, start agent, pause, reset

All chaos goes through this server (not straight to the mocks), so a phone on the
venue Wi-Fi only needs to reach this one port.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from store.db import db

ROOT = Path(__file__).resolve().parent.parent
MOCKS = {
    "warehouse": os.getenv("WAREHOUSE_URL", "http://localhost:8001"),
    "orders": os.getenv("ORDERS_URL", "http://localhost:8002"),
    "carrier": os.getenv("CARRIER_URL", "http://localhost:8003"),
}
RECALLS = {"RC-GB-2611": "GB-2611", "RC-OB-3107": "OB-3107"}
ACTIONS = ["hold_inventory", "cancel_order", "intercept_shipment", "notify_customer"]

app = FastAPI(title="Tracer live log")
agent_proc: subprocess.Popen | None = None


def _plain(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return _json(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    return str(v)


def _json(doc: dict) -> dict:
    return {k: _plain(v) for k, v in doc.items() if k != "_id"}


# idempotency key verb -> step action, so every log line can say what was done to whom
VERB_ACTION = {"hold": "hold_inventory", "cancel": "cancel_order",
               "intercept": "intercept_shipment", "notify": "notify_customer"}


def _event(doc: dict) -> dict:
    """An audit entry for the browser, with action + target_id filled in."""
    out = _json(doc)
    key = out.get("step_key") or ""
    parts = key.split(":")
    if len(parts) == 3:
        out.setdefault("action", VERB_ACTION.get(parts[1]))
        out.setdefault("target_id", parts[2])
    detail = out.get("detail") if isinstance(out.get("detail"), dict) else {}
    if not out.get("target_id") and detail.get("target_id"):
        out["target_id"] = detail["target_id"]
        out.setdefault("action", detail.get("action"))
    return out


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/history")
def history(recall_id: str, limit: int = 300):
    rows = list(db.audit_log.find({"recall_id": recall_id}).sort("at", -1).limit(limit))
    return [_event(r) for r in reversed(rows)]


@app.get("/events")
async def events(recall_id: str):
    """Server-sent events from a change stream on audit_log.

    Every step transition writes an audit entry in the same transaction, so
    watching audit_log alone shows the whole timeline, in commit order.
    """
    pipeline = [{"$match": {"operationType": "insert", "fullDocument.recall_id": recall_id}}]

    async def gen():
        stream = db.audit_log.watch(pipeline, max_await_time_ms=1000)
        try:
            yield ": connected\n\n"
            while True:
                change = await asyncio.to_thread(stream.try_next)
                if change is None:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(_event(change['fullDocument']))}\n\n"
        finally:
            stream.close()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/state")
def state(recall_id: str):
    counts = {a: {} for a in ACTIONS}
    for row in db.steps.aggregate([
        {"$match": {"recall_id": recall_id}},
        {"$group": {"_id": {"a": "$action", "s": "$status"}, "n": {"$sum": 1}}},
    ]):
        counts.setdefault(row["_id"]["a"], {})[row["_id"]["s"]] = row["n"]
    control = db.control.find_one({"recall_id": recall_id}) or {}
    pickup_at, chaos = None, {}
    try:
        ships = httpx.get(f"{MOCKS['carrier']}/shipments", params={"lot": RECALLS.get(recall_id, "")},
                          timeout=2).json()
        pickup_at = min((s["pickup_at"] for s in ships), default=None)
        for name, url in MOCKS.items():
            chaos[name] = httpx.get(f"{url}/chaos", timeout=2).json()
    except httpx.HTTPError:
        pass
    return {
        "counts": counts,
        "control": _json({k: control.get(k) for k in ("status", "paused", "lease_owner", "report", "started_at", "finished_at")}),
        "pickup_at": pickup_at,
        "chaos": chaos,
        "agent_running": agent_proc is not None and agent_proc.poll() is None,
        "agent_exit_code": agent_proc.poll() if agent_proc is not None else None,
        "memory_fixes": db.experiences.count_documents({}),
    }


@app.get("/steps")
def steps(recall_id: str):
    """Every step of a recall, for the world panel (one tile per case, order, truck, household)."""
    fields = {"seq": 1, "action": 1, "target_id": 1, "status": 1, "attempts": 1, "last_error": 1, "proof": 1}
    return [_json(s) for s in db.steps.find({"recall_id": recall_id}, fields).sort("seq", 1)]


def _recall_numbers(rid: str) -> dict:
    """Counted from audit_log and steps in Atlas, the same way scripts/compare_recalls.py does."""
    ev = lambda event, **q: db.audit_log.count_documents({"recall_id": rid, "event": event, **q})
    c = db.control.find_one({"recall_id": rid}) or {}
    seconds = None
    if c.get("started_at") and c.get("finished_at"):
        seconds = round((c["finished_at"] - c["started_at"]).total_seconds())
    return {
        "recall_id": rid,
        "verified": db.steps.count_documents({"recall_id": rid, "status": "verified"}),
        "total": db.steps.count_documents({"recall_id": rid}),
        "llm_calls": ev("repair_proposed", **{"detail.source": "llm"}),
        "memory_hits": ev("memory_hit") or ev("repair_proposed", **{"detail.source": "memory"}),
        "failed_attempts": ev("failed"),
        "repairs_verified": ev("repair_verified"),
        "reconciled": ev("reconciled"),
        "seconds": seconds,
        "status": c.get("status"),
    }


@app.get("/compare")
def compare():
    """Recall 1 vs recall 2: did memory remove the LLM call and the trial and error?"""
    return {"recalls": [_recall_numbers(r) for r in RECALLS],
            "memory_fixes": [_json(f) for f in db.experiences.find(
                {}, {"situation": 1, "uses": 1, "source_recall": 1, "created_at": 1})]}


# ---------- buttons ----------

class ChaosIn(BaseModel):
    service: str
    body: dict


@app.post("/chaos")
def chaos(req: ChaosIn):
    if req.service not in MOCKS:
        raise HTTPException(404, "unknown service")
    return httpx.post(f"{MOCKS[req.service]}/chaos", json=req.body, timeout=3).json()


class PickupChaosIn(BaseModel):
    recall_id: str


@app.post("/chaos/pickup")
def chaos_pickup(req: PickupChaosIn):
    """The truck arrives early: the orders system ships the last open order of the lot,
    and the carrier puts that order's label-printed shipment on a truck. Tracer's cancel
    for that order will now get 409 "already shipped" and must escalate to an intercept."""
    lot = RECALLS.get(req.recall_id)
    if not lot:
        raise HTTPException(404, "unknown recall")
    order = httpx.post(f"{MOCKS['orders']}/pickup", json={"lot": lot}, timeout=3)
    if order.status_code != 200:
        raise HTTPException(order.status_code, order.json().get("detail", "orders pickup failed"))
    order_doc = order.json()
    shipment = httpx.post(f"{MOCKS['carrier']}/pickup", json={"tracking_no": order_doc["tracking_no"]}, timeout=3)
    if shipment.status_code != 200:
        raise HTTPException(shipment.status_code, shipment.json().get("detail", "carrier pickup failed"))
    return {"order": order_doc, "shipment": shipment.json()}


class StartIn(BaseModel):
    recall_id: str = "RC-GB-2611"
    step_delay_ms: int = 600
    crash_at_seq: int | None = None


@app.post("/agent/start")
def start_agent(req: StartIn):
    global agent_proc
    if agent_proc is not None and agent_proc.poll() is None:
        raise HTTPException(409, "agent already running")
    env = {**os.environ, "STEP_DELAY_MS": str(req.step_delay_ms)}
    env.pop("CRASH_AT_SEQ", None)
    if req.crash_at_seq:
        env["CRASH_AT_SEQ"] = str(req.crash_at_seq)
    log = open(ROOT / "agent.log", "ab")
    agent_proc = subprocess.Popen(
        [sys.executable, "-m", "agent.run", "--recall", req.recall_id, "--store", "atlas"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    return {"pid": agent_proc.pid}


@app.post("/agent/kill")
def kill_agent():
    """kill -9: no cleanup, no goodbye. Exactly the crash the agent must survive."""
    if agent_proc is None or agent_proc.poll() is not None:
        raise HTTPException(409, "agent not running")
    os.kill(agent_proc.pid, signal.SIGKILL)
    agent_proc.wait(timeout=5)
    return {"killed": agent_proc.pid}


class CrashIn(BaseModel):
    recall_id: str


@app.post("/agent/crash")
def crash_agent(req: CrashIn):
    """Crash mid-step: the agent dies right after its next API call, before the read-back.
    That leaves one step truly half-done, which is the case reconcile exists for."""
    if agent_proc is None or agent_proc.poll() is not None:
        raise HTTPException(409, "agent not running")
    db.control.update_one({"recall_id": req.recall_id}, {"$set": {"crash": True}})
    return {"crash_requested": True}


class PauseIn(BaseModel):
    recall_id: str
    paused: bool


@app.post("/pause")
def pause(req: PauseIn):
    db.control.update_one({"recall_id": req.recall_id}, {"$set": {"paused": req.paused}})
    return {"paused": req.paused}


class ResetIn(BaseModel):
    wipe_memory: bool = False


@app.post("/reset")
def reset(req: ResetIn):
    if agent_proc is not None and agent_proc.poll() is None:
        raise HTTPException(409, "stop the agent first")
    for url in MOCKS.values():
        httpx.post(f"{url}/reset", timeout=5)
    for coll in ["steps", "audit_log", "control"] + (["experiences"] if req.wipe_memory else []):
        db[coll].delete_many({})
    return {"reset": True, "wiped_memory": req.wipe_memory}
