"""Reset everything before a demo run.

    python -m scripts.reset_demo                 # reset mocks + clear steps, audit_log, control
    python -m scripts.reset_demo --wipe-memory   # ...and also clear experiences (learned fixes)

Memory is kept by default: recall 2 is supposed to find the fix recall 1 saved.
Wipe it only to start the whole story over.
"""
import argparse
import os

import httpx

from store.db import db

MOCKS = {
    "warehouse": os.getenv("WAREHOUSE_URL", "http://localhost:8001"),
    "orders": os.getenv("ORDERS_URL", "http://localhost:8002"),
    "carrier": os.getenv("CARRIER_URL", "http://localhost:8003"),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wipe-memory", action="store_true", help="also delete learned fixes")
    args = parser.parse_args()

    for name, url in MOCKS.items():
        try:
            httpx.post(f"{url}/reset", timeout=5).raise_for_status()
            print(f"  reset    {name} mock")
        except httpx.HTTPError as e:
            print(f"  SKIPPED  {name} mock ({e.__class__.__name__}: is it running?)")

    # delete_many, not drop: dropping would also remove the validators and indexes.
    for coll in ["steps", "audit_log", "control"] + (["experiences"] if args.wipe_memory else []):
        n = db[coll].delete_many({}).deleted_count
        print(f"  cleared  {coll} ({n} docs)")
    if not args.wipe_memory:
        print(f"  kept     experiences ({db.experiences.count_documents({})} learned fixes)")


if __name__ == "__main__":
    main()
