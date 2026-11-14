"""Reconcile after a crash: check the real system instead of replaying or skipping.

A step left in `sent` means "we may have acted". We were saved as `sent`
before the call (write-ahead), so the database knows exactly which steps are
in doubt. For each one we read the record back by idempotency key:

  effect is there        → verified, proof.source = "reconcile". Never call again.
  effect is missing      → failed, so the loop resends it with the SAME key.
  more than one notice   → failed + duplicate_detected (should never happen).

Why this matters: the notify system does not de-duplicate (like real SMS and
email gateways). Replaying would send a second notice; skipping could send none.
"""

from __future__ import annotations

from typing import Any, Callable

from agent.clients import ReadBack, Systems
from agent.config import Recall
from agent.store_api import Step, StoreAPI, now


def step_values(recall: Recall, step: Step) -> dict[str, Any]:
    """Values for filling CallSpec placeholders."""
    return {
        "target_id": step["target_id"],
        "recall_id": recall.recall_id,
        "lot": recall.lot,
        "reason": recall.reason,
        "message": recall.message,
        "idempotency_key": step["idempotency_key"],
    }


def proof_from(read_back: ReadBack, step: Step, source: str) -> dict[str, Any]:
    return {
        "read_back_id": read_back.read_back_id or step["target_id"],
        "observed_status": read_back.observed,
        "source": source,
        "at": now(),
    }


def reconcile(
    recall: Recall,
    store: StoreAPI,
    systems: Systems,
    specs: dict[str, dict[str, Any]],
    actor: str,
    log: Callable[..., None],
) -> int:
    """Settle every `sent` step. Returns how many were found already done."""
    found_done = 0
    for step in store.steps_in_status(recall.recall_id, "sent"):
        spec = specs[step["action"]]
        read_back = systems.read_back(step["system"], spec, step_values(recall, step))
        if read_back.duplicate:
            store.mark_failed(step, f"duplicate: {read_back.observed}", actor,
                              event="duplicate_detected")
            log(step, "DUPLICATE", read_back.observed)
        elif read_back.verified:
            store.mark_verified(step, proof_from(read_back, step, "reconcile"), actor,
                                event="reconciled")
            log(step, "verified", f"proof: {read_back.observed} (reconcile, not resent)")
            found_done += 1
        else:
            reason = read_back.error or read_back.observed
            store.mark_failed(step, f"reconcile: effect not found ({reason})", actor)
            log(step, "failed", "reconcile: not landed before crash, will resend with same key")
    return found_done
