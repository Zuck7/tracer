"""Prove every affected customer got exactly one notice.

    python -m scripts.check_exactly_once RC-GB-2611

Compares the notify system's own records (the real system) with the agent's
verified notify steps in Atlas. Both must agree: one notice per customer.
"""
import os
import sys
from collections import Counter

import httpx

from store.db import db

CARRIER = os.getenv("CARRIER_URL", "http://localhost:8003")


def main(recall_id: str) -> int:
    sent = httpx.get(f"{CARRIER}/notifications", params={"recall_id": recall_id}, timeout=5).json()
    per_customer = Counter(n["customer_id"] for n in sent)
    expected = {s["target_id"] for s in db.steps.find({"recall_id": recall_id, "action": "notify_customer"})}
    verified = db.steps.count_documents({"recall_id": recall_id, "action": "notify_customer",
                                         "status": "verified"})

    dupes = {c: n for c, n in per_customer.items() if n > 1}
    missing = sorted(expected - set(per_customer))
    extra = sorted(set(per_customer) - expected)

    print(f"{recall_id}: {len(expected)} customers in scope, {len(sent)} notices sent, "
          f"{verified} notify steps verified in Atlas")
    if dupes:
        print(f"  ❌ duplicates: {dupes}")
    if missing:
        print(f"  ❌ never notified: {missing}")
    if extra:
        print(f"  ❌ notified but not in scope: {extra}")
    ok = not dupes and not missing and not extra and verified == len(expected)
    print("  ✅ exactly one notice per customer" if ok else "  ❌ NOT exactly once")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "RC-GB-2611"))
