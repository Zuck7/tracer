"""Side-by-side proof that memory helped: recall 1 vs recall 2, straight from Atlas.

    python -m scripts.compare_recalls                       # RC-GB-2611 vs RC-OB-3107
    python -m scripts.compare_recalls RC-GB-2611 RC-OB-3107

Recall 1 meets the carrier API change cold: it fails, asks the LLM, tests the fix,
saves it. Recall 2 meets the same change and finds the fix in Atlas Vector Search.
Everything below is counted from audit_log and steps, not from the agent's own memory.
"""
import sys

from store.db import db

ROWS = [
    ("steps verified", lambda r: f"{_count_steps(r, 'verified')}/{_count_steps(r)}"),
    ("failed attempts", lambda r: _events(r, "failed")),
    # The agent logs repair_proposed for both sources; detail.source says which one.
    ("LLM repair calls", lambda r: _events(r, "repair_proposed", **{"detail.source": "llm"})),
    ("fixes found in memory", lambda r: _events(r, "memory_hit")
        or _events(r, "repair_proposed", **{"detail.source": "memory"})),
    ("repairs verified + saved", lambda r: _events(r, "repair_verified")),
    ("repairs rejected", lambda r: _events(r, "repair_rejected")),
    ("reconciled after crash", lambda r: _events(r, "reconciled")),
    ("carrier attempts (intercepts)", lambda r: _attempts(r, "intercept_shipment")),
    ("run time (s)", lambda r: _duration(r)),
]


def _count_steps(rid: str, status: str | None = None) -> int:
    q = {"recall_id": rid}
    if status:
        q["status"] = status
    return db.steps.count_documents(q)


def _events(rid: str, event: str, **extra) -> int:
    return db.audit_log.count_documents({"recall_id": rid, "event": event, **extra})


def _attempts(rid: str, action: str) -> str:
    rows = list(db.steps.aggregate([
        {"$match": {"recall_id": rid, "action": action}},
        {"$group": {"_id": None, "attempts": {"$sum": "$attempts"}, "steps": {"$sum": 1}}},
    ]))
    return f"{rows[0]['attempts']} for {rows[0]['steps']} steps" if rows else "-"


def _duration(rid: str) -> str:
    c = db.control.find_one({"recall_id": rid}) or {}
    if c.get("started_at") and c.get("finished_at"):
        return f"{(c['finished_at'] - c['started_at']).total_seconds():.0f}"
    return "-"


def main(r1: str, r2: str) -> None:
    w = max(len(label) for label, _ in ROWS)
    print(f"{'':{w}}  {r1:>18}  {r2:>18}")
    for label, fn in ROWS:
        print(f"{label:{w}}  {str(fn(r1)):>18}  {str(fn(r2)):>18}")
    fixes = list(db.experiences.find({}, {"situation": 1, "uses": 1, "source_recall": 1}))
    print(f"\nexperiences in Atlas: {len(fixes)}")
    for f in fixes:
        print(f"  learned in {f.get('source_recall')}, reused {f.get('uses', 0)}x: {f['situation']}")


if __name__ == "__main__":
    args = sys.argv[1:] or ["RC-GB-2611", "RC-OB-3107"]
    main(*args[:2])
