"""In-process fake business systems for testing the agent without the real mocks.

Follows docs/03-contracts.md §2-5 and the recall-1 seed in §7, served through
httpx.MockTransport (no ports, no processes). Role C's FastAPI mocks in mocks/
are the real thing for the demo; this file only lets the agent be tested alone.
"""

from __future__ import annotations

import json
from itertools import count

import httpx

HOSTS = {
    "warehouse": "http://warehouse.test",
    "orders": "http://orders.test",
    "carrier": "http://carrier.test",
    "notify": "http://carrier.test",
}


class FakeSystems:
    def __init__(self) -> None:
        self.units = {f"UNIT-{i:04d}": {"unit_id": f"UNIT-{i:04d}", "sku": "GRB-OAT-12",
                                        "lot": "GB-2611", "status": "available", "hold": None}
                      for i in range(1, 21)}
        self.units["UNIT-0099"] = {"unit_id": "UNIT-0099", "sku": "GRB-OAT-12", "lot": "GB-2612",
                                   "status": "available", "hold": None}
        self.orders: dict[str, dict] = {}
        for i in range(1, 23):
            status = "open" if i <= 10 else "shipped" if i <= 18 else "delivered"
            tracking = f"TRK-{5000 + i - 10}" if 11 <= i <= 18 else (
                f"TRK-{4990 + i - 19}" if i > 18 else None)
            self.orders[f"ORD-{1000 + i}"] = {
                "order_id": f"ORD-{1000 + i}", "customer_id": f"CUST-{100 + i}", "lot": "GB-2611",
                "status": status, "tracking_no": tracking, "cancel": None}
        self.shipments = {f"TRK-{5000 + i}": {"tracking_no": f"TRK-{5000 + i}",
                                              "order_id": f"ORD-{1010 + i}", "lot": "GB-2611",
                                              "status": "in_transit", "intercept": None}
                          for i in range(1, 9)}
        self.notifications: list[dict] = []
        self.keys: dict[str, dict] = {}  # idempotency key → first response
        self.drop_next_writes = {"warehouse": 0, "orders": 0, "carrier": 0}
        self.calls: list[tuple[str, str]] = []
        self._ids = count(1)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- routing -------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.split(".")[0]
        path = request.url.path
        parts = path.strip("/").split("/")
        self.calls.append((request.method, f"{host}{path}"))
        body = json.loads(request.content) if request.content else {}
        key = request.headers.get("Idempotency-Key")
        q = request.url.params

        if request.method == "GET":
            return self._get(host, parts, q)
        if host == "carrier" and parts == ["notifications"]:
            return self._notify(body, key)  # never de-duplicated, never dropped here
        if key in self.keys:
            return httpx.Response(200, json=self.keys[key])
        if self.drop_next_writes.get(host, 0) > 0:  # chaos: 200, nothing changes
            self.drop_next_writes[host] -= 1
            return httpx.Response(200, json={"status": "ok"})
        return self._write(host, parts, body, key)

    def _get(self, host: str, parts: list[str], q) -> httpx.Response:
        if host == "warehouse" and parts == ["inventory"]:
            lot = q.get("lot")  # fuzzy search: GB-2611 also returns GB-2612
            items = [u for u in self.units.values() if u["lot"][:5] == lot[:5]]
            return httpx.Response(200, json=[_public(u, "hold") for u in items])
        if host == "warehouse" and parts[0] == "inventory":
            return _found(self.units.get(parts[1]))
        if host == "orders" and parts == ["orders"]:
            items = [o for o in self.orders.values() if o["lot"] == q.get("lot")]
            return httpx.Response(200, json=[_public(o, "cancel") for o in items])
        if host == "orders":
            return _found(self.orders.get(parts[1]))
        if host == "carrier" and parts == ["shipments"]:
            items = [s for s in self.shipments.values() if s["lot"] == q.get("lot")]
            return httpx.Response(200, json=[_public(s, "intercept") for s in items])
        if host == "carrier" and parts[0] == "shipments":
            return _found(self.shipments.get(parts[1]))
        if host == "carrier" and parts == ["notifications"]:
            field, value = next(iter(q.items()))
            return httpx.Response(200, json=[n for n in self.notifications if n.get(field) == value])
        return httpx.Response(404, json={"detail": "Not Found"})

    def _write(self, host: str, parts: list[str], body: dict, key: str) -> httpx.Response:
        if host == "warehouse" and parts[-1] == "hold":
            unit = self.units.get(parts[1])
            if not unit:
                return httpx.Response(404, json={"detail": "unit not found"})
            hold_id = f"HOLD-{next(self._ids):04x}"
            unit.update(status="hold", hold={"hold_id": hold_id, "recall_id": body["recall_id"],
                                             "idempotency_key": key})
            response = {"unit_id": unit["unit_id"], "status": "hold", "hold_id": hold_id}
        elif host == "orders" and parts[-1] == "cancel":
            order = self.orders.get(parts[1])
            if not order:
                return httpx.Response(404, json={"detail": "order not found"})
            if order["status"] in ("shipped", "delivered"):
                return httpx.Response(409, json={"detail": "order already shipped"})
            order.update(status="cancelled", cancel={"recall_id": body["recall_id"],
                                                     "idempotency_key": key})
            response = {"order_id": order["order_id"], "status": "cancelled"}
        elif host == "carrier" and parts == ["intercepts"]:
            shipment = self.shipments.get(body.get("tracking_no"))
            if not shipment:
                return httpx.Response(404, json={"detail": "shipment not found"})
            shipment.update(status="intercept_requested",
                            intercept={"recall_id": body["recall_id"], "idempotency_key": key})
            response = {"intercept_id": f"INT-{next(self._ids):04x}",
                        "status": "intercept_requested"}
        else:
            return httpx.Response(404, json={"detail": "Not Found"})
        self.keys[key] = response
        return httpx.Response(200, json=response)

    def _notify(self, body: dict, key: str) -> httpx.Response:
        notice = {"notification_id": f"NTF-{next(self._ids):04x}", "idempotency_key": key, **body}
        self.notifications.append(notice)
        return httpx.Response(200, json={"notification_id": notice["notification_id"]})

    # -- checks used by tests ------------------------------------------------
    def notices_per_customer(self, recall_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for n in self.notifications:
            if n["recall_id"] == recall_id:
                counts[n["customer_id"]] = counts.get(n["customer_id"], 0) + 1
        return counts


def _public(record: dict, hidden: str) -> dict:
    return {k: v for k, v in record.items() if k != hidden}


def _found(record: dict | None) -> httpx.Response:
    if record is None:
        return httpx.Response(404, json={"detail": "not found"})
    return httpx.Response(200, json=record)
