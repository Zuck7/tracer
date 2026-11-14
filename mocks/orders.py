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
