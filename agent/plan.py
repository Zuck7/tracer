"""Scoping and planning: ask each system where the lot is, decide per item, build the steps.

The lot can be in four places, and each needs a different action:

    warehouse shelves    → hold the case
    open orders          → cancel the order (it hasn't left yet)
    shipped orders       → too late to cancel: intercept the shipment on the truck
    delivered orders     → the product is in a home: notify the customer
    every customer with an order of the lot, whatever its status → exactly one notice

Order matters: holds → cancels → intercepts → notifications. Stop the product
spreading first, then tell people. Within each group, targets are sorted so the
same data always gives the same seq numbers.

Every candidate goes through the policy gate. A refused target never becomes a
step. The `scope` returned here records what was found and what was decided for
every item, with a plain-English reason, so a human (or a judge) can audit it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent import policy
from agent.callspecs import ACTIONS, idempotency_key
from agent.clients import Systems
from agent.config import Recall

# Only shipments actually on a truck can be intercepted. "label_created" is
# waiting for pickup (its order is still open and gets cancelled instead);
# "intercept_requested" means a previous run already stopped it.
INTERCEPTABLE = {"in_transit", "intercept_requested"}


@dataclass
class Plan:
    steps: list[dict[str, Any]] = field(default_factory=list)
    denied: list[dict[str, Any]] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)

    @property
    def problems(self) -> list[str]:
        return [p for check in self.scope.get("checks", []) for p in check["problems"]]


def shipment_id(shipment: dict[str, Any]) -> str | None:
    """Carrier v1 calls it tracking_no, v2 calls it shipment_ref. Accept either."""
    return shipment.get("tracking_no") or shipment.get("shipment_ref")


def _order_decision(order: dict[str, Any], carrier_ids: set[str]) -> tuple[str, str]:
    status, trk = order.get("status"), order.get("tracking_no")
    if status == "open":
        return "cancel", "Not shipped yet: cancel the order before the truck collects it."
    if status == "shipped":
        if trk in carrier_ids:
            return "intercept_shipment", f"Already on a truck ({trk}): too late to cancel, " \
                                         "so Tracer stops the shipment instead."
        return "needs_human", f"Order says shipped ({trk or 'no tracking number'}) but the " \
                              "carrier has no such shipment. A human must find this parcel."
    if status == "delivered":
        return "notify_only", "Already delivered: the product is in a home, so the customer " \
                              "gets a notice."
    if status == "cancelled":
        return "none", "Already cancelled."
    return "needs_human", f"Unknown order status {status!r}."


def _carrier_decision(shipment: dict[str, Any]) -> tuple[str, str]:
    status = shipment.get("status")
    if status in INTERCEPTABLE:
        return "intercept", "On a truck: ask the carrier to bring it back."
    if status == "delivered":
        return "skip", "Already delivered: nothing to intercept, the customer gets a notice."
    if status == "label_created":
        return "skip", "Label printed but not collected yet: the order is cancelled instead."
    return "skip", f"Status {status!r}: not on a truck."


def build_plan(recall: Recall, systems: Systems) -> Plan:
    lot = {"lot": recall.lot}
    units = sorted(systems.get_json("warehouse", "/inventory", lot), key=lambda u: u["unit_id"])
    orders = sorted(systems.get_json("orders", "/orders", lot), key=lambda o: o["order_id"])
    shipments = sorted(systems.get_json("carrier", "/shipments", lot),
                       key=lambda s: shipment_id(s) or "")
    carrier_ids = {shipment_id(s) for s in shipments if shipment_id(s)}

    plan = Plan()
    scope: dict[str, Any] = {"lot": recall.lot, "warehouse": [], "orders": [], "carrier": [],
                             "customers": [], "checks": []}
    candidates: list[tuple[str, str, str | None]] = []  # (action, target_id, target_lot)

    def gate(system: str, action: str, target_lot: str | None) -> policy.Decision:
        return policy.check(recall, system, action, target_lot)

    for u in units:
        d = gate("warehouse", "hold_inventory", u.get("lot"))
        if d.allow:
            candidates.append(("hold_inventory", u["unit_id"], u.get("lot")))
            item = ("hold", "On a shelf: put it on hold so it can't be picked or shipped.")
        else:
            item = ("refuse", f"Lot {u.get('lot')} is a different lot. Not touched.")
        scope["warehouse"].append({"id": u["unit_id"], "lot": u.get("lot"),
                                   "status": u.get("status"), "decision": item[0],
                                   "reason": item[1]})

    for o in orders:
        if not gate("orders", "cancel_order", o.get("lot")).allow:
            decision, reason = "refuse", f"Lot {o.get('lot')} is a different lot."
        else:
            decision, reason = _order_decision(o, carrier_ids)
            if decision == "cancel":
                candidates.append(("cancel_order", o["order_id"], o.get("lot")))
        scope["orders"].append({"id": o["order_id"], "customer_id": o.get("customer_id"),
                                "lot": o.get("lot"), "status": o.get("status"),
                                "tracking_no": o.get("tracking_no"), "decision": decision,
                                "reason": reason})

    for s in shipments:
        sid = shipment_id(s)
        if not gate("carrier", "intercept_shipment", s.get("lot")).allow:
            decision, reason = "refuse", f"Lot {s.get('lot')} is a different lot."
        else:
            decision, reason = _carrier_decision(s)
            if decision == "intercept":
                candidates.append(("intercept_shipment", sid, s.get("lot")))
        scope["carrier"].append({"id": sid, "order_id": s.get("order_id"), "lot": s.get("lot"),
                                 "status": s.get("status"), "decision": decision,
                                 "reason": reason})

    # Every customer with an order of the lot, whatever the order status.
    customers: dict[str, dict[str, Any]] = {}
    for o in orders:
        customers.setdefault(o["customer_id"], o)
    for cid in sorted(customers):
        o = customers[cid]
        if gate("notify", "notify_customer", o.get("lot")).allow:
            candidates.append(("notify_customer", cid, o.get("lot")))
            oid = o["order_id"]
            reason = {
                "open": f"Ordered from this lot ({oid}). The order is being cancelled, "
                        "and they should know why.",
                "shipped": f"Their order ({oid}) is on a truck to them.",
                "delivered": f"Their order ({oid}) was delivered: the product is in their home.",
            }.get(o.get("status"), f"Ordered from this lot ({oid}).")
            scope["customers"].append({"id": cid, "order_id": oid,
                                       "order_status": o.get("status"), "decision": "notify",
                                       "reason": reason})

    scope["checks"] = _checks(recall, orders, carrier_ids)
    plan.scope = scope

    for action, target_id, target_lot in candidates:
        system = ACTIONS[action][0]
        decision = policy.check(recall, system, action, target_lot)
        if not decision.allow:
            plan.denied.append({"system": system, "action": action, "target_id": target_id,
                                "lot": target_lot, "reason": decision.reason})
            continue
        plan.steps.append(new_step(recall, len(plan.steps) + 1, action, target_id, target_lot))
    # Refusals seen while scoping (e.g. the decoy case) are recorded too.
    for item in scope["warehouse"]:
        if item["decision"] == "refuse":
            plan.denied.append({"system": "warehouse", "action": "hold_inventory",
                                "target_id": item["id"], "lot": item["lot"],
                                "reason": f"lot {item['lot']} is outside recall scope "
                                          f"({recall.lot})"})
    return plan


def _checks(recall: Recall, orders: list[dict[str, Any]], carrier_ids: set) -> list[dict]:
    """Cross-check two systems that don't talk to each other: an order the order system
    calls shipped must be a shipment the carrier knows, or that parcel is invisible."""
    shipped = [o for o in orders if o.get("status") == "shipped" and o.get("lot") == recall.lot]
    unmatched = [o for o in shipped if o.get("tracking_no") not in carrier_ids]
    return [{
        "name": "Every shipped order is on a shipment the carrier knows about",
        "ok": not unmatched,
        "summary": f"{len(shipped) - len(unmatched)} of {len(shipped)} shipped orders matched "
                   "to a truck",
        "problems": [f"{o['order_id']} says shipped ({o.get('tracking_no') or 'no tracking'}) "
                     "but the carrier has no such shipment" for o in unmatched],
    }]


def cross_check(recall: Recall, systems: Systems) -> list[dict]:
    lot = {"lot": recall.lot}
    orders = systems.get_json("orders", "/orders", lot)
    ids = {shipment_id(s) for s in systems.get_json("carrier", "/shipments", lot)}
    return _checks(recall, orders, ids)


def new_step(recall: Recall, seq: int, action: str, target_id: str,
             target_lot: str | None) -> dict[str, Any]:
    return {
        "recall_id": recall.recall_id,
        "seq": seq,
        "system": ACTIONS[action][0],
        "action": action,
        "target_id": target_id,
        "lot": target_lot,
        "status": "planned",
        "idempotency_key": idempotency_key(recall.recall_id, action, target_id),
        "attempts": 0,
        "last_error": None,
    }
