"""Carrier + notifications mock, port 8003.  uvicorn mocks.carrier:app --port 8003

Two API versions. v1 is the default. POST /chaos {"api_version": "v2"} switches to v2:
  - the id field is renamed tracking_no -> shipment_ref everywhere
  - POST /intercepts is gone (plain 404, no hint)
  - the new call is POST /shipments/{shipment_ref}/return
Notifications are not versioned and deliberately do NOT de-duplicate.
"""
from fastapi import Header, HTTPException
from pydantic import BaseModel

from mocks import seed
from mocks.common import make_app, short_id

app, state = make_app("carrier", lambda: {"shipments": seed.shipments(), "notifications": []})


def _v2() -> bool:
    return state.chaos["api_version"] == "v2"


def _out(s: dict) -> dict:
    """Serialize a shipment in the current API version's field names."""
    d = dict(s)
    if _v2():
        d["shipment_ref"] = d.pop("tracking_no")
    return d


def _shipment(ship_id: str) -> dict:
    s = state.data["shipments"].get(ship_id)
    if s is None:
        raise HTTPException(404, f"shipment {ship_id} not found")
    return s


def _intercept(s: dict, recall_id: str, idempotency_key: str, id_key: str, prefix: str) -> dict:
    new_id = short_id(prefix)

    def apply():
        if s["status"] != "in_transit":
            raise HTTPException(409, f"shipment is {s['status']}, cannot intercept")
        s["status"] = "intercept_requested"
        s["intercept"] = {id_key: new_id, "recall_id": recall_id, "idempotency_key": idempotency_key}
        return {id_key: new_id, "status": "intercept_requested"}

    return state.write(idempotency_key, apply,
                       fake=lambda: {id_key: new_id, "status": "intercept_requested"})


# ---------- shipments (versioned) ----------

@app.get("/shipments")
def list_shipments(lot: str):
    return [{k: v for k, v in _out(s).items() if k != "intercept"}
            for s in state.data["shipments"].values() if s["lot"] == lot]


@app.get("/shipments/{ship_id}")
def read_shipment(ship_id: str):
    return _out(_shipment(ship_id))


class InterceptV1(BaseModel):
    tracking_no: str
    recall_id: str
    reason: str = ""


@app.post("/intercepts")
def intercept_v1(body: InterceptV1, idempotency_key: str | None = Header(default=None)):
    if _v2():
        raise HTTPException(404, "Not Found")
    return _intercept(_shipment(body.tracking_no), body.recall_id, idempotency_key, "intercept_id", "INT")


class ReturnV2(BaseModel):
    recall_id: str
    reason: str = ""


@app.post("/shipments/{shipment_ref}/return")
def return_v2(shipment_ref: str, body: ReturnV2, idempotency_key: str | None = Header(default=None)):
    if not _v2():
        raise HTTPException(404, "Not Found")
    return _intercept(_shipment(shipment_ref), body.recall_id, idempotency_key, "return_id", "RET")


SPEC_V1 = {
    "service": "carrier", "version": "v1",
    "endpoints": [
        {"method": "GET", "path": "/shipments?lot={lot}", "returns": "list of shipments",
         "fields": ["tracking_no", "order_id", "lot", "status", "pickup_at"]},
        {"method": "GET", "path": "/shipments/{tracking_no}", "returns": "one shipment",
         "fields": ["tracking_no", "order_id", "lot", "status", "pickup_at", "intercept"]},
        {"method": "POST", "path": "/intercepts", "purpose": "stop a shipment in transit",
         "headers": ["Idempotency-Key"], "body": {"tracking_no": "string", "recall_id": "string", "reason": "string"},
         "returns": {"intercept_id": "string", "status": "intercept_requested"}},
    ],
    "status_values": ["in_transit", "intercept_requested", "delivered"],
}

SPEC_V2 = {
    "service": "carrier", "version": "v2",
    "changes_from_v1": "tracking_no renamed to shipment_ref; intercepts are now returns on the shipment resource",
    "endpoints": [
        {"method": "GET", "path": "/shipments?lot={lot}", "returns": "list of shipments",
         "fields": ["shipment_ref", "order_id", "lot", "status", "pickup_at"]},
        {"method": "GET", "path": "/shipments/{shipment_ref}", "returns": "one shipment",
         "fields": ["shipment_ref", "order_id", "lot", "status", "pickup_at", "intercept"]},
        {"method": "POST", "path": "/shipments/{shipment_ref}/return", "purpose": "stop a shipment in transit",
         "headers": ["Idempotency-Key"], "body": {"recall_id": "string", "reason": "string"},
         "returns": {"return_id": "string", "status": "intercept_requested"}},
    ],
    "status_values": ["in_transit", "intercept_requested", "delivered"],
}


@app.get("/api-spec")
def api_spec():
    return SPEC_V2 if _v2() else SPEC_V1


# ---------- notifications (not versioned, NO de-duplication) ----------

class NotifyIn(BaseModel):
    customer_id: str
    recall_id: str
    channel: str = "email"
    message: str = ""


@app.post("/notifications")
def send_notification(body: NotifyIn, idempotency_key: str | None = Header(default=None)):
    notification_id = short_id("NTF")

    def apply():
        state.data["notifications"].append({
            "notification_id": notification_id, "customer_id": body.customer_id,
            "recall_id": body.recall_id, "channel": body.channel, "message": body.message,
            "idempotency_key": idempotency_key, "sent_at": seed._iso(seed.datetime.now(seed.timezone.utc)),
        })
        return {"notification_id": notification_id}

    return state.write_no_dedupe(idempotency_key, apply, fake=lambda: {"notification_id": notification_id})


@app.get("/notifications")
def list_notifications(idempotency_key: str | None = None, recall_id: str | None = None,
                       customer_id: str | None = None):
    out = state.data["notifications"]
    if idempotency_key:
        out = [n for n in out if n["idempotency_key"] == idempotency_key]
    if recall_id:
        out = [n for n in out if n["recall_id"] == recall_id]
    if customer_id:
        out = [n for n in out if n["customer_id"] == customer_id]
    return out


class PickupIn(BaseModel):
    tracking_no: str


@app.post("/pickup")
def pickup(body: PickupIn):
    """Chaos: the label-printed shipment is collected and is now on a truck."""
    with state.lock:
        shipment = _shipment(body.tracking_no)
        shipment["status"] = "in_transit"
    print(f"[carrier] CHAOS: {body.tracking_no} picked up early, now in transit")
    return _out(shipment)
