"""Prove a recall finished: compare every business system with what Tracer wrote in Atlas.

    python -m scripts.prove_recall RC-GB-2611

Read-only. It never changes a system or the database. Two independent sources
must agree for every item:

  the systems   warehouse, orders, carrier, notifications: their own records
  Atlas         Tracer's steps (each with proof), audit log and report

If Tracer claimed something the systems don't show, or a system shows something
Tracer didn't account for, it is printed as ❌. This is the answer to "how do you
know it worked?": not because the agent says so, but because every system says so.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from typing import Any

import httpx

from agent.config import get_recall
from agent.plan import shipment_id
from store.db import db

URLS = {
    "warehouse": os.getenv("WAREHOUSE_URL", "http://localhost:8001"),
    "orders": os.getenv("ORDERS_URL", "http://localhost:8002"),
    "carrier": os.getenv("CARRIER_URL", "http://localhost:8003"),
}


def _get(system: str, path: str, **params: Any) -> Any:
    response = httpx.get(f"{URLS[system]}{path}", params=params, timeout=5)
    response.raise_for_status()
    return response.json()


def prove(recall_id: str) -> dict[str, Any]:
    recall = get_recall(recall_id)
    lot = recall.lot
    checks: list[dict[str, Any]] = []

    def check(area: str, claim: str, ok: bool, detail: str = "") -> None:
        checks.append({"area": area, "claim": claim, "ok": bool(ok), "detail": detail})

    steps = {s["idempotency_key"]: s for s in db.steps.find({"recall_id": recall_id})}
    by_action: dict[str, list[dict]] = {}
    for s in steps.values():
        by_action.setdefault(s["action"], []).append(s)
    verified = lambda action: [s for s in by_action.get(action, []) if s["status"] == "verified"]

    # -- warehouse ------------------------------------------------------------
    units = _get("warehouse", "/inventory", lot=lot)
    in_lot = [u for u in units if u["lot"] == lot]
    other = [u for u in units if u["lot"] != lot]
    held = []
    for u in in_lot:
        record = _get("warehouse", f"/inventory/{u['unit_id']}")
        if record["status"] == "hold" and (record.get("hold") or {}).get("recall_id") == recall_id:
            held.append(u["unit_id"])
    check("Warehouse", f"every case of lot {lot} is on hold for this recall",
          len(held) == len(in_lot), f"{len(held)} of {len(in_lot)} on hold (warehouse records)")
    v_holds = {s["target_id"] for s in verified("hold_inventory")}
    check("Warehouse", "Tracer's confirmed holds are exactly the cases the warehouse shows held",
          v_holds == set(held), f"{len(v_holds)} confirmed in Atlas")
    untouched = [u["unit_id"] for u in other if u["status"] != "hold"]
    check("Warehouse", "cases from other lots were left alone",
          len(untouched) == len(other) and not any(s["target_id"] in untouched
                                                   for s in steps.values()),
          ", ".join(f"{u['unit_id']} ({u['lot']}) {u['status']}" for u in other) or "none found")

    # -- orders + carrier -----------------------------------------------------
    orders = [o for o in _get("orders", "/orders", lot=lot) if o["lot"] == lot]
    ships = {shipment_id(s): s for s in _get("carrier", "/shipments", lot=lot) if s["lot"] == lot}
    still_open = [o["order_id"] for o in orders if o["status"] == "open"]
    check("Orders", "no order of the lot is still open", not still_open,
          ", ".join(still_open) or f"{sum(o['status'] == 'cancelled' for o in orders)} cancelled")
    cancelled = {o["order_id"] for o in orders if o["status"] == "cancelled"}
    v_cancels = {s["target_id"] for s in verified("cancel_order")}
    check("Orders", "Tracer's confirmed cancels match the order system", v_cancels == cancelled,
          f"{len(v_cancels)} confirmed in Atlas, {len(cancelled)} cancelled in the order system")
    moving = [sid for sid, s in ships.items() if s["status"] == "in_transit"]
    check("Trucks", "no shipment of the lot is still moving", not moving,
          ", ".join(moving) or f"{sum(s['status'] == 'intercept_requested' for s in ships.values())}"
                               " being brought back")
    stopped = {sid for sid, s in ships.items() if s["status"] == "intercept_requested"}
    v_intercepts = {s["target_id"] for s in verified("intercept_shipment")}
    check("Trucks", "Tracer's confirmed intercepts match the carrier", v_intercepts == stopped,
          f"{len(v_intercepts)} confirmed in Atlas, {len(stopped)} in the carrier's records")
    shipped = [o for o in orders if o["status"] in ("shipped",)]
    lost = [o["order_id"] for o in shipped if o.get("tracking_no") not in ships]
    check("Trucks", "every shipped order is a shipment the carrier knows", not lost,
          ", ".join(lost) or f"{len(shipped)} shipped orders matched")

    # -- customers ------------------------------------------------------------
    customers = {o["customer_id"] for o in orders}
    notices = Counter(n["customer_id"] for n in
                      _get("carrier", "/notifications", recall_id=recall_id))
    missing = sorted(customers - set(notices))
    twice = sorted(c for c, n in notices.items() if n > 1)
    extra = sorted(set(notices) - customers)
    check("Customers", "every customer who bought the lot got a notice", not missing,
          ", ".join(missing) or f"{len(customers)} of {len(customers)}")
    check("Customers", "nobody got two notices", not twice,
          ", ".join(f"{c} ×{notices[c]}" for c in twice) or "0 duplicates")
    check("Customers", "nobody outside the lot was notified", not extra, ", ".join(extra) or "none")

    # -- Atlas: Tracer's own records ------------------------------------------
    no_proof = db.steps.count_documents({"recall_id": recall_id, "status": "verified",
                                         "proof": {"$exists": False}})
    check("Atlas", "every confirmed action carries proof", no_proof == 0,
          f"{sum(s['status'] == 'verified' for s in steps.values())} with proof")
    events = Counter(a["step_key"] for a in db.audit_log.find(
        {"recall_id": recall_id, "event": {"$in": ["verified", "reconciled"]}}, {"step_key": 1})
        if a.get("step_key"))
    if not events:  # Atlas audit entries carry step_id, not step_key
        ids = Counter(a["step_id"] for a in db.audit_log.find(
            {"recall_id": recall_id, "event": {"$in": ["verified", "reconciled"]}}, {"step_id": 1}))
        unlogged = [s["seq"] for s in steps.values() if s["status"] == "verified"
                    and ids.get(s["_id"], 0) != 1]
    else:
        unlogged = [s["seq"] for s in steps.values() if s["status"] == "verified"
                    and events.get(s["idempotency_key"], 0) != 1]
    check("Atlas", "every confirmed action has exactly one confirmation in the audit log",
          not unlogged, ", ".join(f"#{n}" for n in sorted(unlogged)) or "all match")
    report = (db.control.find_one({"recall_id": recall_id}) or {}).get("report") or {}
    claimed = (report.get("customers_notified") or {}).get("verified")
    check("Atlas", "the report's figures match the systems",
          report and claimed == len(customers) - len(missing)
          and report.get("duplicate_notifications") == len(twice),
          f"report says {claimed} notified, {report.get('duplicate_notifications')} duplicates"
          if report else "no report yet")

    return {"recall_id": recall_id, "lot": lot, "ok": all(c["ok"] for c in checks),
            "checks": checks}


def main(recall_id: str) -> int:
    result = prove(recall_id)
    print(f"\nProof for {recall_id} (lot {result['lot']}): every system vs Tracer's records in Atlas\n")
    area = None
    for c in result["checks"]:
        if c["area"] != area:
            area = c["area"]
            print(f"  {area}")
        print(f"    {'✅' if c['ok'] else '❌'} {c['claim']}  ({c['detail']})")
    print("\n  " + ("✅ PROVEN: every system agrees with Tracer." if result["ok"]
                    else "❌ NOT PROVEN: see the ❌ lines above."))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "RC-GB-2611"))
