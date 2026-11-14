"""AtlasStore: the real store behind agent/store_api.py's StoreAPI.

The agent loads this with `load_store("atlas")`. Every function matches the
StoreAPI signature exactly, so the agent can't tell it apart from MemoryStore,
except that state now survives a kill -9 because it lives in MongoDB Atlas.
"""
from datetime import timedelta
from typing import Any

from pymongo import ASCENDING, ReturnDocument

from store import steps as S
from store.db import db, now

Step = dict[str, Any]


class AtlasStore:
    # ---------- control: one document per recall (lease, pause flag, report) ----------

    def ensure_control(self, recall_id: str, lot: str) -> None:
        # $setOnInsert: create the doc on the first run, leave it alone on a restart.
        db.control.update_one(
            {"recall_id": recall_id},
            {"$setOnInsert": {"recall_id": recall_id, "lot": lot, "status": "running", "paused": False,
                              "lease_owner": None, "lease_expires": None, "started_at": now(),
                              "finished_at": None, "report": None}},
            upsert=True,
        )

    def claim_lease(self, recall_id: str, worker_id: str, ttl_s: int = 15) -> bool:
        """Atomic claim. Wins if the lease is free, already ours, or expired.

        find_one_and_update checks and writes in one step on the server, so two
        workers racing for the same recall can't both win.
        """
        t = now()
        doc = db.control.find_one_and_update(
            {"recall_id": recall_id,
             "$or": [{"lease_owner": None}, {"lease_owner": worker_id}, {"lease_expires": {"$lt": t}}]},
            {"$set": {"lease_owner": worker_id, "lease_expires": t + timedelta(seconds=ttl_s),
                      "status": "running"}},
            return_document=ReturnDocument.AFTER,
        )
        return doc is not None

    def renew_lease(self, recall_id: str, worker_id: str, ttl_s: int = 15) -> bool:
        result = db.control.update_one(
            {"recall_id": recall_id, "lease_owner": worker_id},
            {"$set": {"lease_expires": now() + timedelta(seconds=ttl_s)}},
        )
        return result.matched_count == 1

    def is_paused(self, recall_id: str) -> bool:
        doc = db.control.find_one({"recall_id": recall_id}, {"paused": 1})
        return bool(doc and doc.get("paused"))

    def finish(self, recall_id: str, report: dict[str, Any]) -> None:
        db.control.update_one(
            {"recall_id": recall_id},
            {"$set": {"status": "done", "finished_at": now(), "report": report, "lease_owner": None}},
        )

    # ---------- steps: each transition writes the step and its audit entry together ----------

    def count_steps(self, recall_id: str) -> int:
        return db.steps.count_documents({"recall_id": recall_id})

    def insert_plan(self, steps: list[Step]) -> int:
        return S.insert_plan(steps)

    def next_steps(self, recall_id: str, max_attempts: int) -> list[Step]:
        return list(db.steps.find({"recall_id": recall_id,
                                   "status": {"$in": ["planned", "failed"]},
                                   "attempts": {"$lt": max_attempts}}).sort("seq", ASCENDING))

    def steps_in_status(self, recall_id: str, status: str) -> list[Step]:
        return S.steps_in_status(recall_id, status)

    @staticmethod
    def _id(step: Step):
        if "_id" in step:
            return step["_id"]
        found = db.steps.find_one({"idempotency_key": step["idempotency_key"]}, {"_id": 1})
        if not found:
            raise ValueError(f"no step with key {step['idempotency_key']}")
        return found["_id"]

    def mark_sent(self, step: Step, actor: str, policy_reason: str) -> Step:
        # 'sent' -> 'sent' is allowed: a reconcile resend of a step whose effect wasn't found.
        return S._transition(
            self._id(step), ["planned", "failed", "sent"],
            {"$set": {"status": "sent", "updated_at": now()}, "$inc": {"attempts": 1}},
            "sent", actor, policy_decision="allow", policy_reason=policy_reason,
            detail={"attempt": step.get("attempts", 0) + 1},
        )

    def mark_verified(self, step: Step, proof: dict[str, Any], actor: str,
                      event: str = "verified") -> Step:
        return S._transition(
            self._id(step), ["sent"],
            {"$set": {"status": "verified", "proof": proof, "last_error": None, "updated_at": now()}},
            event, actor, detail={"proof": proof},
        )

    def mark_failed(self, step: Step, error: str, actor: str, event: str = "failed") -> Step:
        return S._transition(
            self._id(step), ["sent"],
            {"$set": {"status": "failed", "last_error": error, "updated_at": now()}},
            event, actor, detail={"error": error},
        )

    def status_counts(self, recall_id: str) -> dict[str, dict[str, int]]:
        """{'hold_inventory': {'verified': 20}, 'notify_customer': {'verified': 21, 'sent': 1}, ...}"""
        out: dict[str, dict[str, int]] = {}
        pipeline = [{"$match": {"recall_id": recall_id}},
                    {"$group": {"_id": {"action": "$action", "status": "$status"}, "n": {"$sum": 1}}}]
        for row in db.steps.aggregate(pipeline):
            out.setdefault(row["_id"]["action"], {})[row["_id"]["status"]] = row["n"]
        return out

    # ---------- audit entries that aren't a step transition ----------

    def log_event(self, recall_id: str, event: str, actor: str, step: Step | None = None,
                  **fields: Any) -> None:
        S.audit(recall_id, event, step, actor, **fields)

    def count_events(self, recall_id: str, event: str) -> int:
        return db.audit_log.count_documents({"recall_id": recall_id, "event": event})
