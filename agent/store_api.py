"""The only door from the agent to its state.

`StoreAPI` lists every store function the agent calls. B's `store/` package
(MongoDB Atlas) implements it for real; `MemoryStore` below is a stand-in with
the same functions so the loop can run before Atlas is wired up.

Why one door: every status change and its audit entry must be written in one
transaction. If only `store/` touches MongoDB, there's one place to get that
right, and the agent can't accidentally write a step without its audit line.

Contract each implementation must keep:
  * mark_sent / mark_verified / mark_failed write the step change AND its
    audit_log entry together (one transaction in Atlas), and return the
    updated step.
  * mark_sent increments `attempts`.
  * insert_plan ignores duplicate idempotency keys (unique index).
  * next_steps returns planned/failed steps with attempts < max_attempts,
    ordered by seq.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

Step = dict[str, Any]


def now() -> datetime:
    return datetime.now(timezone.utc)


class StoreAPI(Protocol):
    # control: one document per recall (lease, pause flag, report)
    def ensure_control(self, recall_id: str, lot: str) -> None: ...
    def claim_lease(self, recall_id: str, worker_id: str, ttl_s: int = 15) -> bool: ...
    def renew_lease(self, recall_id: str, worker_id: str, ttl_s: int = 15) -> bool: ...
    def is_paused(self, recall_id: str) -> bool: ...
    # Optional: demo crash on demand. True once per request (consumed atomically).
    def take_crash_request(self, recall_id: str) -> bool: ...
    def finish(self, recall_id: str, report: dict[str, Any]) -> None: ...

    # steps: each transition writes the step and its audit entry together
    def count_steps(self, recall_id: str) -> int: ...
    def insert_plan(self, steps: list[Step]) -> int: ...
    def next_steps(self, recall_id: str, max_attempts: int) -> list[Step]: ...
    def steps_in_status(self, recall_id: str, status: str) -> list[Step]: ...
    def mark_sent(self, step: Step, actor: str, policy_reason: str) -> Step: ...
    def mark_verified(self, step: Step, proof: dict[str, Any], actor: str,
                      event: str = "verified") -> Step: ...
    def mark_failed(self, step: Step, error: str, actor: str, event: str = "failed") -> Step: ...
    def status_counts(self, recall_id: str) -> dict[str, dict[str, int]]: ...

    # audit entries that aren't a step transition (denied, paused, lease_claimed, ...)
    def log_event(self, recall_id: str, event: str, actor: str, step: Step | None = None,
                  **fields: Any) -> None: ...
    def count_events(self, recall_id: str, event: str) -> int: ...


class MemoryStore:
    """Stand-in store. Optionally saved to a JSON file after every write, so a
    killed process can be restarted and resume (for testing crash recovery
    before Atlas is ready). Not for the demo: Atlas is the real store."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.steps: dict[str, Step] = {}  # by idempotency_key
        self.audit: list[dict[str, Any]] = []
        self.control: dict[str, dict[str, Any]] = {}
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text())
            self.steps, self.audit, self.control = data["steps"], data["audit"], data["control"]

    # -- persistence ---------------------------------------------------------
    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {"steps": self.steps, "audit": self.audit, "control": self.control}
        tmp.write_text(json.dumps(payload, default=str, indent=1))
        os.replace(tmp, self.path)  # atomic, so a kill never leaves half a file

    # -- control -------------------------------------------------------------
    def ensure_control(self, recall_id: str, lot: str) -> None:
        self.control.setdefault(recall_id, {
            "recall_id": recall_id, "lot": lot, "status": "running", "paused": False,
            "lease_owner": None, "lease_expires": None, "started_at": now().isoformat(),
            "finished_at": None, "report": None,
        })
        self._save()

    def claim_lease(self, recall_id: str, worker_id: str, ttl_s: int = 15) -> bool:
        doc = self.control[recall_id]
        expires = doc["lease_expires"]
        expired = expires is None or datetime.fromisoformat(expires) < now()
        if doc["lease_owner"] in (None, worker_id) or expired:
            doc["lease_owner"] = worker_id
            doc["lease_expires"] = (now() + timedelta(seconds=ttl_s)).isoformat()
            self._save()
            return True
        return False

    def renew_lease(self, recall_id: str, worker_id: str, ttl_s: int = 15) -> bool:
        if self.control[recall_id]["lease_owner"] != worker_id:
            return False
        self.control[recall_id]["lease_expires"] = (now() + timedelta(seconds=ttl_s)).isoformat()
        self._save()
        return True

    def is_paused(self, recall_id: str) -> bool:
        return bool(self.control.get(recall_id, {}).get("paused"))

    def take_crash_request(self, recall_id: str) -> bool:
        doc = self.control.get(recall_id, {})
        if doc.get("crash"):
            doc["crash"] = False
            self._save()
            return True
        return False

    def finish(self, recall_id: str, report: dict[str, Any]) -> None:
        doc = self.control[recall_id]
        doc.update(status="done", finished_at=now().isoformat(), report=report, lease_owner=None)
        self._save()

    # -- steps ---------------------------------------------------------------
    def count_steps(self, recall_id: str) -> int:
        return sum(1 for s in self.steps.values() if s["recall_id"] == recall_id)

    def insert_plan(self, steps: list[Step]) -> int:
        inserted = 0
        for step in steps:
            if step["idempotency_key"] in self.steps:
                continue  # same as the unique index rejecting a duplicate
            doc = copy.deepcopy(step)
            doc["created_at"] = doc["updated_at"] = now().isoformat()
            self.steps[doc["idempotency_key"]] = doc
            self._audit(doc["recall_id"], "planned", "tracer-agent", doc)
            inserted += 1
        self._save()
        return inserted

    def next_steps(self, recall_id: str, max_attempts: int) -> list[Step]:
        rows = [s for s in self.steps.values() if s["recall_id"] == recall_id
                and s["status"] in ("planned", "failed") and s["attempts"] < max_attempts]
        return [copy.deepcopy(s) for s in sorted(rows, key=lambda s: s["seq"])]

    def steps_in_status(self, recall_id: str, status: str) -> list[Step]:
        rows = [s for s in self.steps.values()
                if s["recall_id"] == recall_id and s["status"] == status]
        return [copy.deepcopy(s) for s in sorted(rows, key=lambda s: s["seq"])]

    def _transition(self, step: Step, changes: dict[str, Any], event: str, actor: str,
                    **audit_fields: Any) -> Step:
        doc = self.steps[step["idempotency_key"]]
        doc.update(changes, updated_at=now().isoformat())
        self._audit(doc["recall_id"], event, actor, doc, **audit_fields)
        self._save()  # step + audit saved together, like one transaction
        return copy.deepcopy(doc)

    def mark_sent(self, step: Step, actor: str, policy_reason: str) -> Step:
        attempts = self.steps[step["idempotency_key"]]["attempts"] + 1
        return self._transition(step, {"status": "sent", "attempts": attempts}, "sent", actor,
                                policy_decision="allow", policy_reason=policy_reason,
                                detail={"attempt": attempts})

    def mark_verified(self, step: Step, proof: dict[str, Any], actor: str,
                      event: str = "verified") -> Step:
        return self._transition(step, {"status": "verified", "proof": proof, "last_error": None},
                                event, actor, detail={"proof": proof})

    def mark_failed(self, step: Step, error: str, actor: str, event: str = "failed") -> Step:
        return self._transition(step, {"status": "failed", "last_error": error}, event, actor,
                                detail={"error": error})

    def status_counts(self, recall_id: str) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for s in self.steps.values():
            if s["recall_id"] == recall_id:
                per_action = counts.setdefault(s["action"], {})
                per_action[s["status"]] = per_action.get(s["status"], 0) + 1
        return counts

    # -- audit ---------------------------------------------------------------
    def _audit(self, recall_id: str, event: str, actor: str, step: Step | None = None,
               **fields: Any) -> None:
        self.audit.append({
            "recall_id": recall_id,
            "step_key": step["idempotency_key"] if step else None,
            "seq": step["seq"] if step else None,
            "actor": actor, "event": event, "at": now().isoformat(), **fields,
        })

    def log_event(self, recall_id: str, event: str, actor: str, step: Step | None = None,
                  **fields: Any) -> None:
        self._audit(recall_id, event, actor, step, **fields)
        self._save()

    def count_events(self, recall_id: str, event: str) -> int:
        return sum(1 for a in self.audit if a["recall_id"] == recall_id and a["event"] == event)


MEMORY_STORE_PATH = Path(__file__).resolve().parent.parent / ".tracer" / "memstore.json"


def load_store(kind: str = "auto") -> StoreAPI:
    """'atlas' → B's store/ package; 'memory' → MemoryStore saved to .tracer/;
    'auto' → Atlas if store/ exists, else memory."""
    if kind in ("atlas", "auto"):
        try:
            from store import agent_store  # type: ignore[import-not-found]
        except ImportError:
            if kind == "atlas":
                raise SystemExit("store/ (Atlas layer) isn't available yet. Use --store memory.")
        else:
            return agent_store.AtlasStore()
    return MemoryStore(MEMORY_STORE_PATH)
