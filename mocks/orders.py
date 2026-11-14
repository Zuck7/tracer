"""Orders mock, port 8002.  uvicorn mocks.orders:app --port 8002"""
from fastapi import Header, HTTPException
from pydantic import BaseModel

from mocks import seed
from mocks.common import make_app

app, state = make_app("orders", lambda: {"orders": seed.orders()})


class CancelIn(BaseModel):
    recall_id: str
    reason: str = ""


def _order(order_id: str) -> dict:
    order = state.data["orders"].get(order_id)
    if order is None:
        raise HTTPException(404, f"order {order_id} not found")
    return order


@app.get("/stats")
def stats():
    """Size of the whole order book, so the recalled lot can be shown against normal traffic."""
    orders = state.data["orders"].values()
    by_status: dict[str, int] = {}
    for o in orders:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
    return {"orders": len(orders), "customers": len({o["customer_id"] for o in orders}),
            "lots": len({o["lot"] for o in orders}), "by_status": by_status}


@app.get("/orders")
def list_orders(lot: str):
    return [{k: v for k, v in o.items() if k != "cancel"}
            for o in state.data["orders"].values() if o["lot"] == lot]


@app.get("/orders/{order_id}")
def read_order(order_id: str):
    return _order(order_id)


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, body: CancelIn, idempotency_key: str | None = Header(default=None)):
    order = _order(order_id)

    def apply():
        if order["status"] in ("shipped", "delivered"):
            raise HTTPException(409, "order already shipped")
        order["status"] = "cancelled"
        order["cancel"] = {"recall_id": body.recall_id, "idempotency_key": idempotency_key}
        return {"order_id": order_id, "status": "cancelled"}

    return state.write(idempotency_key, apply,
                       fake=lambda: {"order_id": order_id, "status": "cancelled"})


class PickupIn(BaseModel):
    lot: str


@app.post("/pickup")
def pickup(body: PickupIn):
    """Chaos: the truck arrives early and collects the LAST open order of the lot
    (the one the agent reaches last), so its cancel will get 409 "already shipped"."""
    with state.lock:
        open_orders = sorted((o for o in state.data["orders"].values()
                              if o["lot"] == body.lot and o["status"] == "open"),
                             key=lambda o: o["order_id"])
        if not open_orders:
            raise HTTPException(409, "no open orders left to pick up")
        order = open_orders[-1]
        order["status"] = "shipped"
    print(f"[orders] CHAOS: truck picked up {order['order_id']} early ({order['tracking_no']})")
    return {"order_id": order["order_id"], "tracking_no": order["tracking_no"], "status": "shipped"}
