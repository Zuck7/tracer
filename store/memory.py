"""Vector memory of verified fixes (collection `experiences`).

    situation = situation_text("carrier", "intercept_shipment", "POST", "/intercepts", 404, "Not Found")
    hit = find_fix("carrier", situation)          # None, or {"fix": CallSpec, "score": 0.97, ...}
    save_fix("carrier", "intercept_shipment", situation, fix, source_recall="RC-GB-2611")

Why vector search instead of an exact match: error text varies (IDs, paths,
wording). A similar situation should still find the fix that worked before.
Atlas Automated Embedding turns `situation` into a vector with Voyage AI on
insert and at query time, so there is no embedding code here.

Only call save_fix AFTER the fix passed a read-back. Memory holds proven fixes only.
"""
import os
from typing import Any

from store.db import db, now

INDEX = "experience_situation"
# Measured in the sandbox (voyage-4): same failure 0.93, same failure + different ID 0.90,
# same endpoint + different error 0.86, different system 0.71. 0.88 keeps only the first two.
# A new fix becomes searchable ~5 s after save_fix (embedding is generated asynchronously).
MIN_SCORE = float(os.getenv("MEMORY_MIN_SCORE", "0.88"))


def situation_text(system: str, action: str, method: str, path: str, status: int, detail: str) -> str:
    """Built the same way every time, so the same failure gives the same text and a strong match."""
    return f"{system} {action}: {method} {path} returned {status} {detail}".strip()


def find_fix(system: str, situation: str, min_score: float = MIN_SCORE) -> dict[str, Any] | None:
    """Best verified fix for this system whose situation is similar enough, else None."""
    pipeline = [
        {"$vectorSearch": {
            "index": INDEX,
            "path": "situation",
            "query": {"text": situation},
            "filter": {"system": system, "verified": True},
            "numCandidates": 50,
            "limit": 3,
        }},
        {"$project": {"fix": 1, "situation": 1, "action": 1, "source_recall": 1, "uses": 1,
                      "score": {"$meta": "vectorSearchScore"}}},
    ]
    results = list(db.experiences.aggregate(pipeline))
    best = results[0] if results else None
    if best and best["score"] >= min_score:
        db.experiences.update_one({"_id": best["_id"]}, {"$inc": {"uses": 1}})
        return best
    return None


def save_fix(system: str, action: str, situation: str, fix: dict[str, Any], source_recall: str) -> bool:
    """Save a fix that passed read-back. Returns False if this exact situation is already stored."""
    result = db.experiences.update_one(
        # verified: True in the filter, so a retired fix for the same situation is left as
        # history and the new fix is stored as a fresh document.
        {"system": system, "action": action, "situation": situation, "verified": True},
        {"$setOnInsert": {"system": system, "action": action, "situation": situation, "fix": fix,
                          "verified": True, "source_recall": source_recall, "uses": 0,
                          "created_at": now()}},
        upsert=True,
    )
    return result.upserted_id is not None


def retire_fix(system: str, situation: str, reason: str) -> bool:
    """Stop trusting the remembered fix for this situation. Call it when a fix from memory
    fails its read-back test: the system has changed again, so the memory is now wrong.

    The fix is kept (verified: False, with the reason and time) so a reviewer can see what
    the agent used to believe, but find_fix only returns verified fixes, so it is never
    used again. Returns False if there was nothing to retire.
    """
    hit = find_fix(system, situation)
    if not hit:
        return False
    db.experiences.update_one(
        {"_id": hit["_id"]},
        {"$set": {"verified": False, "retired_at": now(), "retired_reason": reason}, "$inc": {"uses": -1}})
    return True
