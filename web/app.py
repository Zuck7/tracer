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
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from agent.config import RECALLS as CATALOGUE
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
agent_recall: str | None = None   # which recall the running agent process belongs to


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


@app.get("/console")
def console():
    """Client console: declare a recall, approve the plan, follow the run, resolve, report."""
    return FileResponse(Path(__file__).parent / "console.html")


@app.get("/judge")
def judge():
    """Phone page for judges: the chaos buttons and a counter, nothing else."""
    return FileResponse(Path(__file__).parent / "judge.html")


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


_world_cache: dict = {"at": 0.0, "value": None}


def _world() -> dict:
    """How big the systems Tracer searched are: {warehouse, orders, carrier} from each mock's
    /stats, or None for a system that didn't answer. Cached 10 s, because both pages poll
    /state every second and these numbers barely move."""
    if time.monotonic() - _world_cache["at"] < 10 and _world_cache["value"] is not None:
        return _world_cache["value"]
    out = {}
    for name in ("warehouse", "orders", "carrier"):
        try:
            r = httpx.get(f"{MOCKS[name]}/stats", timeout=2)
            out[name] = r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            out[name] = None
    _world_cache.update(at=time.monotonic(), value=out)
    return out


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
        "control": _json({k: control.get(k) for k in ("status", "paused", "lease_owner", "report", "started_at", "finished_at",
                                                    "approval", "acknowledged")}),
        "pickup_at": pickup_at,
        "chaos": chaos,
        "agent_running": agent_proc is not None and agent_proc.poll() is None,
        "agent_exit_code": agent_proc.poll() if agent_proc is not None else None,
        "agent_recall": agent_recall,
        "memory_fixes": db.experiences.count_documents({}),
        "world": _world(),
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


# ---------- client console ----------

def _agent_running_for(recall_id: str) -> bool:
    return agent_proc is not None and agent_proc.poll() is None and agent_recall == recall_id


@app.get("/catalogue")
def catalogue():
    """What can be recalled: product, lot, hazard, and the exact notice customers will get."""
    return [{"recall_id": r.recall_id, "lot": r.lot, "product": r.product, "hazard": r.hazard,
             "reason": r.reason, "message": r.message} for r in CATALOGUE.values()]


@app.get("/recalls")
def recalls():
    """Every catalogue recall with where it stands, for the console's sidebar."""
    out = []
    for r in CATALOGUE.values():
        control = db.control.find_one({"recall_id": r.recall_id}) or {}
        counts = {row["_id"]: row["n"] for row in db.steps.aggregate([
            {"$match": {"recall_id": r.recall_id}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
        out.append({"recall_id": r.recall_id, "lot": r.lot, "product": r.product, "hazard": r.hazard,
                    "status": control.get("status"), "paused": bool(control.get("paused")),
                    "approval": _plain(control.get("approval")), "counts": counts,
                    "needs": len((control.get("report") or {}).get("needs_attention") or []),
                    "handed": len(control.get("acknowledged") or []),
                    "agent_running": _agent_running_for(r.recall_id)})
    return out


class ApproveIn(BaseModel):
    recall_id: str
    approved_by: str = "Recall coordinator"


@app.post("/approve")
def approve(req: ApproveIn):
    """The human gate: until this is called the agent has only looked, never written."""
    result = db.control.update_one(
        {"recall_id": req.recall_id, "approval.state": "pending"},
        {"$set": {"paused": False,
                  "approval": {"state": "approved", "by": req.approved_by.strip() or "Recall coordinator",
                               "at": datetime.now(timezone.utc)}}})
    if result.matched_count == 0:
        raise HTTPException(409, "this recall is not waiting for approval")
    return {"approved": True}


class RecallIn(BaseModel):
    recall_id: str


@app.post("/cancel-recall")
def cancel_recall(req: RecallIn):
    """Cancel before approval. Nothing was sent to any business system, so removing the
    plan leaves the world exactly as it was."""
    global agent_proc
    control = db.control.find_one({"recall_id": req.recall_id}) or {}
    if (control.get("approval") or {}).get("state") != "pending":
        raise HTTPException(409, "only a recall that is waiting for approval can be cancelled")
    if db.steps.count_documents({"recall_id": req.recall_id, "status": {"$ne": "planned"}}):
        raise HTTPException(409, "this recall has already started acting")
    if _agent_running_for(req.recall_id):
        os.kill(agent_proc.pid, signal.SIGKILL)
        agent_proc.wait(timeout=5)
        agent_proc = None
    for coll in ("steps", "audit_log", "control"):
        db[coll].delete_many({"recall_id": req.recall_id})
    return {"cancelled": True}


class AcknowledgeIn(BaseModel):
    recall_id: str
    seq: int | None = None
    target_id: str | None = None
    note: str = ""


@app.post("/acknowledge")
def acknowledge(req: AcknowledgeIn):
    """A person takes over one item the agent could not finish."""
    entry = {"seq": req.seq, "target_id": req.target_id, "note": req.note.strip(),
             "at": datetime.now(timezone.utc)}
    result = db.control.update_one({"recall_id": req.recall_id}, {"$push": {"acknowledged": entry}})
    if result.matched_count == 0:
        raise HTTPException(404, "unknown recall")
    return {"acknowledged": True}


@app.get("/report")
def report(recall_id: str):
    """The recall effectiveness report as a file the client can keep."""
    control = db.control.find_one({"recall_id": recall_id}) or {}
    if not control.get("report"):
        raise HTTPException(404, "no report yet: it is written when the recall finishes")
    body = {"recall_id": recall_id, "lot": control.get("lot"),
            "approval": _plain(control.get("approval")),
            "handed_to_a_person": _plain(control.get("acknowledged") or []),
            "report": _plain(control["report"]),
            "steps": [_json(s) for s in db.steps.find(
                {"recall_id": recall_id},
                {"seq": 1, "action": 1, "target_id": 1, "status": 1, "attempts": 1,
                 "last_error": 1, "proof": 1, "idempotency_key": 1}).sort("seq", 1)]}
    return JSONResponse(body, headers={
        "Content-Disposition": f'attachment; filename="{recall_id}-recall-report.json"'})


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
    require_approval: bool = False   # console: plan and show the scope, then wait for a human
    retry: bool = False              # console "Try again": give unconfirmed steps fresh attempts


@app.post("/agent/start")
def start_agent(req: StartIn):
    global agent_proc, agent_recall
    if agent_proc is not None and agent_proc.poll() is None:
        raise HTTPException(409, "agent already running")
    if req.recall_id not in CATALOGUE:
        raise HTTPException(404, "unknown recall")
    if req.require_approval:
        # Create the control document paused, before the agent exists. The agent's own
        # ensure_control uses $setOnInsert, so it keeps this one: it plans, records what it
        # found, then waits at its pause check before step 1 until /approve.
        db.control.update_one(
            {"recall_id": req.recall_id},
            {"$setOnInsert": {"recall_id": req.recall_id, "lot": CATALOGUE[req.recall_id].lot,
                              "status": "running", "paused": True, "lease_owner": None,
                              "lease_expires": None, "started_at": datetime.now(timezone.utc),
                              "finished_at": None, "report": None,
                              "approval": {"state": "pending"}}},
            upsert=True)
    env = {**os.environ, "STEP_DELAY_MS": str(req.step_delay_ms)}
    env.pop("CRASH_AT_SEQ", None)
    if req.crash_at_seq:
        env["CRASH_AT_SEQ"] = str(req.crash_at_seq)
    if req.retry:
        # A step that used all its attempts is skipped on a normal restart. A person asking to
        # try again is new information, so raise the limit for this run only: each unconfirmed
        # step gets the usual number of attempts again. Same idempotency keys, so still safe.
        base = int(os.environ.get("MAX_ATTEMPTS", "3"))
        worst = db.steps.find_one({"recall_id": req.recall_id, "status": "failed"}, sort=[("attempts", -1)])
        env["MAX_ATTEMPTS"] = str((worst["attempts"] if worst else 0) + base)
    log = open(ROOT / "agent.log", "ab")
    agent_proc = subprocess.Popen(
        [sys.executable, "-m", "agent.run", "--recall", req.recall_id, "--store", "atlas"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    agent_recall = req.recall_id
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
    control = db.control.find_one({"recall_id": req.recall_id}, {"approval": 1}) or {}
    if not req.paused and (control.get("approval") or {}).get("state") == "pending":
        # Resume must not be a way around the approval gate.
        raise HTTPException(409, "this recall is waiting for the supplier's approval, in the supplier portal")
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


# ---------- Tracer chat (text in, text out: the supplier portal and the voice agent share it) ----------
#
# agent/intake.py understands the supplier and writes Tracer's replies. This section only
# performs what was understood, with the same functions the buttons use, so typing
# "yes go ahead" and pressing Approve go through exactly the same gate.

def _intake():
    try:
        from agent import intake
    except ImportError as exc:
        raise HTTPException(503, f"Tracer's chat is not available yet ({exc})") from exc
    return intake


def _latest_recall() -> str | None:
    """The recall the supplier is most likely talking about when none is named."""
    doc = db.control.find_one({}, {"recall_id": 1}, sort=[("started_at", -1)])
    return doc["recall_id"] if doc else None


def _active(recall_id: str | None) -> dict | None:
    if not recall_id:
        return None
    control = db.control.find_one({"recall_id": recall_id})
    if not control:
        return None
    counts: dict[str, dict[str, int]] = {}
    for row in db.steps.aggregate([
        {"$match": {"recall_id": recall_id}},
        {"$group": {"_id": {"a": "$action", "s": "$status"}, "n": {"$sum": 1}}},
    ]):
        counts.setdefault(row["_id"]["a"], {})[row["_id"]["s"]] = row["n"]
    return {"recall_id": recall_id, "status": control.get("status"),
            "approval": _plain(control.get("approval")), "counts": counts,
            "report": _plain(control.get("report")), "paused": bool(control.get("paused")),
            "agent_running": _agent_running_for(recall_id)}


def _lot_words(recall_id: str) -> str:
    r = CATALOGUE.get(recall_id)
    return f"{r.product}, lot {r.lot}" if r else recall_id


def _perform(intent: str, rid: str | None) -> tuple[bool, str, str | None]:
    """Carry out one understood intent. Returns (ok, what the server did, reply override)."""
    try:
        if intent == "start_recall":
            if rid not in CATALOGUE:
                return False, "no matching recall", "I couldn't match that to one of your lots. Which product and lot is it?"
            control = db.control.find_one({"recall_id": rid}) or {}
            status, approval = control.get("status"), (control.get("approval") or {}).get("state")
            if status == "done":
                return False, "recall already finished", (
                    f"The recall for {_lot_words(rid)} is already finished. Ask me how it went, or for the report.")
            if approval == "pending":
                return True, "already waiting for approval", None
            if status:
                return True, "recall already under way", (
                    f"The recall for {_lot_words(rid)} is already under way. I'll keep you posted here.")
            start_agent(StartIn(recall_id=rid, require_approval=True))
            return True, f"started finding {_lot_words(rid)}; nothing will change until you approve", None
        if not rid:
            if intent in ("approve", "cancel", "pause", "resume"):
                return False, "no active recall", "There's no recall in progress yet. Tell me what happened and which product it is."
            return True, "nothing to do", None
        # For actions, the reply is written here AFTER acting: the LLM only saw the state
        # before the action (e.g. "paused, waiting for approval"), so its wording can be stale.
        if intent == "approve":
            approve(ApproveIn(recall_id=rid, approved_by="Northfield Foods (in chat)"))
            n = db.steps.count_documents({"recall_id": rid})
            return True, "approved; the recall is starting", (
                f"Approved. I'm starting the recall for {_lot_words(rid)} now: {n} actions. "
                "You'll see each one here as it's confirmed, and I'll tell you straight away if anything needs you.")
        if intent == "cancel":
            cancel_recall(RecallIn(recall_id=rid))
            return True, "recall cancelled before anything was changed", (
                f"Cancelled. Nothing was changed for {_lot_words(rid)} in any system.")
        if intent in ("pause", "resume"):
            pause(PauseIn(recall_id=rid, paused=intent == "pause"))
            return True, "paused" if intent == "pause" else "resumed", (
                "Paused. I've stopped before my next action and I'm holding my place. Say \"resume\" when you're ready."
                if intent == "pause" else "Resumed. Carrying on from where I stopped.")
        return True, "nothing to do", None
    except HTTPException as exc:
        detail = str(exc.detail)
        friendly = {
            "agent already running": "I'm still working on another recall. I can start this one as soon as it finishes.",
            "this recall is not waiting for approval": "That recall isn't waiting for approval right now.",
            "this recall has already started acting": "It has already started acting, so it can't be cancelled. I can pause it instead.",
            "only a recall that is waiting for approval can be cancelled": "Only a recall that hasn't started can be cancelled. I can pause it instead.",
        }.get(detail, f"I couldn't do that: {detail}.")
        if "waiting for the supplier's approval" in detail:
            friendly = "It's waiting for your approval first. Say \"go ahead\" or press Approve when you're ready."
        return False, detail, friendly


class ChatMessage(BaseModel):
    role: str   # "supplier" | "tracer"
    text: str


class ChatIn(BaseModel):
    messages: list[ChatMessage]
    recall_id: str | None = None


@app.post("/chat")
def chat(req: ChatIn):
    """One supplier turn: understand it (agent/intake.py), then do it. JSON in, JSON out."""
    intake = _intake()
    rid = req.recall_id if req.recall_id in CATALOGUE else _latest_recall()
    try:
        understood = intake.interpret([m.model_dump() for m in req.messages], _active(rid))
    except Exception as exc:  # noqa: BLE001 - the LLM or network failing must not 500 the chat
        raise HTTPException(502, f"Tracer couldn't understand that just now ({type(exc).__name__}). Please try again.") from exc
    intent = understood.get("intent") or "other"
    if intent == "start_recall":
        rid = understood.get("recall_id")
    ok, action, override = _perform(intent, rid)
    return {"intent": intent, "recall_id": rid, "reply": override or understood.get("reply") or "",
            "action": action, "ok": ok}


def _highlights(recall_id: str) -> list[str]:
    """The moments worth telling the supplier about, from the audit log."""
    out: list[str] = []
    events = list(db.audit_log.find({"recall_id": recall_id}).sort("at", 1))
    target = lambda e: (e.get("step_key") or "::").split(":")[-1]   # noqa: E731
    for e in events:
        d = e.get("detail") or {}
        if e["event"] == "denied":
            out.append(f"{d.get('target_id', 'An item')} from lot {d.get('lot', 'another lot')} turned up in the "
                       "search and was left alone because it is a different lot.")
        elif e["event"] == "failed" and str(d.get("error", "")).startswith("read-back mismatch"):
            out.append(f"The system said OK for {target(e)} but nothing had changed; Tracer caught it by checking "
                       "and did it again.")
        elif e["event"] == "planned" and d.get("escalation"):
            x = d["escalation"]
            out.append(f"Order {x.get('order_id')} left on a truck before it could be cancelled, so Tracer brought "
                       f"its shipment {x.get('tracking_no')} back instead.")
        elif e["event"] == "repair_verified":
            out.append("The carrier changed its system mid-recall. " + (
                "Tracer reused a fix it had learned in an earlier recall, with no AI call."
                if d.get("source") == "memory" else
                "Tracer worked out the new request with Claude, tested it on one shipment, and saved the fix."))
        elif e["event"] == "repair_rejected":
            out.append("A proposed fix for a changed system did not work, so Tracer stopped instead of guessing.")
        elif e["event"] == "duplicate_detected":
            out.append(f"Customer {target(e)} received more than one notice and needs a person.")
    reconciled = sum(1 for e in events if e["event"] == "reconciled")
    if reconciled:
        out.append(f"Tracer was interrupted and restarted; {reconciled} action(s) already done were checked "
                   "and not repeated.")
    return list(dict.fromkeys(out))   # keep order, drop repeats


class ExplainIn(BaseModel):
    recall_id: str


@app.post("/chat/explain")
def chat_explain(req: ExplainIn):
    """Tracer's plain-English account of how the recall went."""
    control = db.control.find_one({"recall_id": req.recall_id}) or {}
    if not control.get("report"):
        raise HTTPException(409, "the recall hasn't finished yet")
    intake = _intake()
    try:
        text = intake.explain(_plain(control["report"]), _highlights(req.recall_id))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Tracer couldn't write the explanation just now ({type(exc).__name__}).") from exc
    return {"recall_id": req.recall_id, "text": text}


@app.get("/lots")
def lots():
    """Northfield Foods' production records, as Tracer sees them."""
    return _intake().supplier_lots()
