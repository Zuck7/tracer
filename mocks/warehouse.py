"""Warehouse mock, port 8001.  uvicorn mocks.warehouse:app --port 8001"""
from fastapi import Header, HTTPException
from pydantic import BaseModel

from mocks import seed
from mocks.common import make_app, short_id

app, state = make_app("warehouse", lambda: {"units": seed.inventory()})


class HoldIn(BaseModel):
    recall_id: str
    reason: str = ""


def _unit(unit_id: str) -> dict:
    unit = state.data["units"].get(unit_id)
    if unit is None:
        raise HTTPException(404, f"unit {unit_id} not found")
    return unit


@app.get("/inventory")
def list_inventory(lot: str):
    # Deliberately fuzzy: matches on the lot prefix, so "GB-2611" also returns the GB-2612 decoy.
    prefix = lot[:5]
    return [{k: v for k, v in u.items() if k != "hold"}
            for u in state.data["units"].values() if u["lot"].startswith(prefix)]


@app.get("/inventory/{unit_id}")
def read_unit(unit_id: str):
    u = _unit(unit_id)
    return {"unit_id": u["unit_id"], "lot": u["lot"], "status": u["status"], "hold": u["hold"]}


@app.post("/inventory/{unit_id}/hold")
def hold_unit(unit_id: str, body: HoldIn, idempotency_key: str | None = Header(default=None)):
    unit = _unit(unit_id)
    hold_id = short_id("HOLD")

    def apply():
        unit["status"] = "hold"
        unit["hold"] = {"hold_id": hold_id, "recall_id": body.recall_id, "idempotency_key": idempotency_key}
        return {"unit_id": unit_id, "status": "hold", "hold_id": hold_id}

    return state.write(idempotency_key, apply,
                       fake=lambda: {"unit_id": unit_id, "status": "hold", "hold_id": hold_id})


@app.get("/stats")
def stats():
    units = state.data["units"].values()
    return {"cases": len(state.data["units"]), "lots": len({u["lot"] for u in units}),
            "skus": len({u["sku"] for u in units})}
