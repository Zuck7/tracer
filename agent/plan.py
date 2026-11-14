"""Scoping and planning: ask each system what the lot touches, then build the steps.

Order matters: holds → cancels → intercepts → notifications. Stop the product
spreading first, then tell people. Within each group, targets are sorted so the
same data always gives the same seq numbers.

Every candidate goes through the policy gate. A denied target never becomes a
step; it only gets an audit entry (returned here as `denied`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent import policy
from agent.callspecs import ACTIONS, idempotency_key
from agent.clients import Systems
from agent.config import Recall

# Shipments in these states can't (or needn't) be intercepted.
_NOT_INTERCEPTABLE = {"delivered", "returned", "cancelled"}


@dataclass
class Plan:
    steps: list[dict[str, Any]] = field(default_factory=list)
    denied: list[dict[str, Any]] = field(default_factory=list)


def shipment_id(shipment: dict[str, Any]) -> str | None:
    """Carrier v1 calls it tracking_no, v2 calls it shipment_ref. Accept either."""
    return shipment.get("tracking_no") or shipment.get("shipment_ref")


def build_plan(recall: Recall, systems: Systems) -> Plan:
    lot = {"lot": recall.lot}
    units = systems.get_json("warehouse", "/inventory", lot)
    orders = systems.get_json("orders", "/orders", lot)
    shipments = systems.get_json("carrier", "/shipments", lot)

    candidates: list[tuple[str, str, str | None]] = []  # (action, target_id, target_lot)
    candidates += [("hold_inventory", u["unit_id"], u.get("lot"))
                   for u in sorted(units, key=lambda u: u["unit_id"])]
    candidates += [("cancel_order", o["order_id"], o.get("lot"))
                   for o in sorted(orders, key=lambda o: o["order_id"])
                   if o.get("status") == "open"]
    candidates += [("intercept_shipment", shipment_id(s), s.get("lot"))
                   for s in sorted(shipments, key=lambda s: shipment_id(s) or "")
                   if shipment_id(s) and s.get("status") not in _NOT_INTERCEPTABLE]
    # Every customer with an order of the lot, whatever the order status.
    customers: dict[str, str | None] = {}
    for order in orders:
        customers.setdefault(order["customer_id"], order.get("lot"))
    candidates += [("notify_customer", cid, customers[cid]) for cid in sorted(customers)]

    plan = Plan()
    for action, target_id, target_lot in candidates:
        system = ACTIONS[action][0]
        decision = policy.check(recall, system, action, target_lot)
        if not decision.allow:
            plan.denied.append({"system": system, "action": action, "target_id": target_id,
                                "lot": target_lot, "reason": decision.reason})
            continue
        plan.steps.append({
            "recall_id": recall.recall_id,
            "seq": len(plan.steps) + 1,
            "system": system,
            "action": action,
            "target_id": target_id,
            "lot": target_lot,
            "status": "planned",
            "idempotency_key": idempotency_key(recall.recall_id, action, target_id),
            "attempts": 0,
            "last_error": None,
        })
    return plan
