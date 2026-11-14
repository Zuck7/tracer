"""Step state machine: planned -> sent -> verified / failed.

Every status change and its audit_log entry are written in ONE transaction,
so the audit trail can never disagree with the step state.

The agent uses these functions and nothing else to change a step:

    insert_plan(steps)                  bulk insert planned steps (safe to call twice)
    next_steps(recall_id)               planned + failed steps, in seq order
    steps_in_status(recall_id, status)  e.g. "sent" for crash reconcile
    mark_sent(step_id)                  BEFORE the API call (write-ahead intent)
    mark_verified(step_id, proof)       only after a read-back matched
    mark_failed(step_id, error)         read-back mismatch or API error
    audit(recall_id, event, ...)        events that aren't step transitions (denied, repair_*, ...)
"""
import os
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import BulkWriteError

from store.db import client, db, now

ACTOR = f"tracer-agent:{os.environ.get('WORKER_ID', 'worker-1')}"


def make_key(recall_id: str, verb: str, target_id: str) -> str:
    """Idempotency key, e.g. RC-GB-2611:notify:CUST-118. Never changes for a step."""
    return f"{recall_id}:{verb}:{target_id}"


def _audit_doc(recall_id: str, event: str, step: dict | None = None, actor: str = ACTOR,
               **extra: Any) -> dict:
    doc = {
        "recall_id": recall_id,
        "step_id": step.get("_id") if step else None,   # None for a denied target (never became a step)
        "step_key": step.get("idempotency_key") if step else None,
        "seq": step.get("seq") if step else None,
        "actor": actor,
        "event": event,
        "at": now(),
    }
    doc.update(extra)
    return doc


def audit(recall_id: str, event: str, step: dict | None = None, actor: str = ACTOR, **extra: Any) -> None:
    """Audit entry on its own, for events that don't change a step (denied, memory_hit, ...)."""
    db.audit_log.insert_one(_audit_doc(recall_id, event, step, actor, **extra))


def insert_plan(steps: list[dict]) -> int:
    """Insert planned steps. Each dict needs recall_id, seq, system, action, target_id, idempotency_key.

    Duplicates (same idempotency_key) are skipped, so re-planning after a crash is harmless.
    Returns the number of new steps inserted.
    """
    if not steps:
        return 0
    t = now()
    docs = [{**s, "status": "planned", "attempts": 0, "last_error": None,
             "created_at": t, "updated_at": t} for s in steps]
    try:
        result = db.steps.insert_many(docs, ordered=False)
        inserted = len(result.inserted_ids)
    except BulkWriteError as e:
        dupes = [err for err in e.details["writeErrors"] if err["code"] == 11000]
        if len(dupes) != len(e.details["writeErrors"]):
            raise
        inserted = e.details["nInserted"]
    if inserted:
        audit(steps[0]["recall_id"], "planned", count=inserted)
    return inserted


def next_steps(recall_id: str) -> list[dict]:
    """Steps still to run: planned or failed, in seq order."""
    return list(db.steps.find({"recall_id": recall_id, "status": {"$in": ["planned", "failed"]}})
                .sort("seq", ASCENDING))


def steps_in_status(recall_id: str, status: str) -> list[dict]:
    return list(db.steps.find({"recall_id": recall_id, "status": status}).sort("seq", ASCENDING))


def get_step(step_id: ObjectId) -> dict | None:
    return db.steps.find_one({"_id": step_id})


def _transition(step_id: ObjectId, from_statuses: list[str], update: dict, event: str,
                actor: str = ACTOR, **audit_extra: Any) -> dict:
    """Change a step's status and write its audit entry in one transaction.

    Raises ValueError if the step isn't in one of from_statuses, so an illegal
    transition (e.g. planned -> verified) can't happen silently.
    """
    def txn(session):
        step = db.steps.find_one_and_update(
            {"_id": step_id, "status": {"$in": from_statuses}},
            update,
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if step is None:
            current = db.steps.find_one({"_id": step_id}, {"status": 1}, session=session)
            raise ValueError(f"step {step_id}: cannot go {current and current['status']} -> {event}")
        db.audit_log.insert_one(_audit_doc(step["recall_id"], event, step, actor, **audit_extra),
                                session=session)
        return step

    with client.start_session() as session:
        return session.with_transaction(txn)


def mark_sent(step_id: ObjectId) -> dict:
    """Call this BEFORE the API call. 'sent' -> 'sent' is allowed for a reconcile resend."""
    return _transition(
        step_id, ["planned", "failed", "sent"],
        {"$set": {"status": "sent", "updated_at": now()}, "$inc": {"attempts": 1}},
        "sent", policy_decision="allow",
    )


def mark_verified(step_id: ObjectId, read_back_id: str, observed_status: str,
                  source: str = "read_back") -> dict:
    """Only after a read-back matched. source is 'read_back' or 'reconcile'."""
    t = now()
    proof = {"read_back_id": read_back_id, "observed_status": observed_status, "source": source, "at": t}
    event = "reconciled" if source == "reconcile" else "verified"
    return _transition(
        step_id, ["sent"],
        {"$set": {"status": "verified", "proof": proof, "last_error": None, "updated_at": t}},
        event, proof=proof,
    )


def mark_failed(step_id: ObjectId, error: str) -> dict:
    return _transition(
        step_id, ["sent"],
        {"$set": {"status": "failed", "last_error": error, "updated_at": now()}},
        "failed", error=error,
    )


def counts(recall_id: str) -> dict[str, int]:
    """{'verified': 58, 'failed': 1, ...} for the live log header and the report."""
    pipeline = [{"$match": {"recall_id": recall_id}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]
    return {row["_id"]: row["n"] for row in db.steps.aggregate(pipeline)}
