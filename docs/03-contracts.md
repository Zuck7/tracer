# 03 · Contracts

**This is the source of truth.** Agent, store and mocks all code against these shapes. Change something here only after telling the other two people.

## 1. Conventions (all services)

| Thing | Value |
|---|---|
| Ports | live log `8000` · warehouse `8001` · orders `8002` · carrier + notify `8003` |
| Base URLs (env) | `WAREHOUSE_URL`, `ORDERS_URL`, `CARRIER_URL`, defaulting to `http://localhost:800x` |
| Format | JSON in, JSON out. Times are ISO 8601 UTC strings over HTTP and BSON dates in Atlas |
| Write header | `Idempotency-Key: <key>` on **every** POST that changes state |
| Errors | FastAPI default `{"detail": "..."}` with a 4xx/5xx status |
| CORS | Mocks allow all origins, so the live log page can call `/chaos` directly |

### Idempotency key format

```
{recall_id}:{verb}:{target_id}
RC-GB-2611:hold:UNIT-0007
RC-GB-2611:cancel:ORD-1004
RC-GB-2611:intercept:TRK-5003
RC-GB-2611:notify:CUST-118
```

### Idempotency behaviour in the mocks

| Endpoint | Same key sent twice |
|---|---|
| Hold, cancel, intercept | Returns the first response again and changes nothing. A dropped write (chaos) does **not** store the key, so a retry applies for real |
| **Notifications** | **No de-duplication.** Every POST creates a new notification. The key is stored on the record so it can be looked up. This is deliberate: real SMS and email gateways don't de-duplicate, so only the agent's reconcile logic keeps it to exactly one |

## 2. Warehouse mock (`:8001`)

### `GET /inventory?lot=GB-2611`
Lists units for a lot. **The search is fuzzy**: it also returns one decoy unit from lot `GB-2612` (for the policy gate).
```json
[
  {"unit_id": "UNIT-0001", "sku": "GRB-OAT-12", "lot": "GB-2611", "location": "A-01-03", "status": "available"},
  {"unit_id": "UNIT-0099", "sku": "GRB-OAT-12", "lot": "GB-2612", "location": "A-01-04", "status": "available"}
]
```

### `POST /inventory/{unit_id}/hold`
Header `Idempotency-Key`. Body:
```json
{"recall_id": "RC-GB-2611", "reason": "Undeclared peanuts, lot GB-2611"}
```
Response `200`:
```json
{"unit_id": "UNIT-0001", "status": "hold", "hold_id": "HOLD-7f3a"}
```

### `GET /inventory/{unit_id}` (read-back)
```json
{"unit_id": "UNIT-0001", "lot": "GB-2611", "status": "hold",
 "hold": {"hold_id": "HOLD-7f3a", "recall_id": "RC-GB-2611", "idempotency_key": "RC-GB-2611:hold:UNIT-0001"}}
```
`hold` is `null` when not held. **Verified when** `status == "hold"` and `hold.recall_id` matches.

## 3. Orders mock (`:8002`)

### `GET /orders?lot=GB-2611`
All orders containing the lot, any status:
```json
[{"order_id": "ORD-1001", "customer_id": "CUST-101", "lot": "GB-2611", "status": "open", "tracking_no": null},
 {"order_id": "ORD-1011", "customer_id": "CUST-111", "lot": "GB-2611", "status": "shipped", "tracking_no": "TRK-5001"},
 {"order_id": "ORD-1019", "customer_id": "CUST-119", "lot": "GB-2611", "status": "delivered", "tracking_no": "TRK-4990"}]
```

### `POST /orders/{order_id}/cancel`
Header `Idempotency-Key`. Body `{"recall_id": "RC-GB-2611", "reason": "..."}`. Response `200`:
```json
{"order_id": "ORD-1001", "status": "cancelled"}
```
Cancelling a `shipped` or `delivered` order returns `409 {"detail": "order already shipped"}`.

### `GET /orders/{order_id}` (read-back)
Same shape as a list item, plus `"cancel": {"recall_id": "...", "idempotency_key": "..."} | null`. **Verified when** `status == "cancelled"`.

## 4. Carrier + notify mock (`:8003`)

The carrier has two API versions. It starts at `v1`. The **change API** chaos switches it to `v2`.

### v1 (default)

| Call | Shape |
|---|---|
| `GET /shipments?lot=GB-2611` | `[{"tracking_no": "TRK-5001", "order_id": "ORD-1011", "lot": "GB-2611", "status": "in_transit", "pickup_at": "..."}]` |
| `POST /intercepts` | Header `Idempotency-Key`. Body `{"tracking_no": "TRK-5001", "recall_id": "...", "reason": "..."}` → `200 {"intercept_id": "INT-91c2", "status": "intercept_requested"}` |
| `GET /shipments/{id}` (read-back) | `{"tracking_no": "TRK-5001", "status": "intercept_requested", "intercept": {"recall_id": "...", "idempotency_key": "..."}}` |

### v2 (after "change API")

| Call | Shape |
|---|---|
| `GET /shipments?lot=GB-2611` | Same, but the field is **`shipment_ref`** instead of `tracking_no` |
| `POST /intercepts` | **`404 {"detail": "Not Found"}`** (no hint, like a real removed route) |
| `POST /shipments/{shipment_ref}/return` | Header `Idempotency-Key`. Body `{"recall_id": "...", "reason": "..."}` → `200 {"return_id": "RET-22aa", "status": "intercept_requested"}` |
| `GET /shipments/{id}` (read-back) | Same as v1 but `shipment_ref` instead of `tracking_no`. Status values unchanged |

### `GET /api-spec`
A short, hand-written description of the **current** version's endpoints and fields, as JSON. This is what the agent gives the LLM during repair. (FastAPI's `/openapi.json` would list both versions, so we don't use it.)

### Notifications (always the same, not versioned)

| Call | Shape |
|---|---|
| `POST /notifications` | Header `Idempotency-Key`. Body `{"customer_id": "CUST-118", "recall_id": "...", "channel": "email", "message": "..."}` → `200 {"notification_id": "NTF-0c41"}`. **No de-duplication** |
| `GET /notifications?idempotency_key=...` (read-back) | List of matching notifications. **Verified when the list length is exactly 1** |
| `GET /notifications?recall_id=...` | All notifications for a recall. Used by the report and the "exactly once" check |

## 5. Chaos and reset (every mock)

| Call | Effect |
|---|---|
| `POST /chaos` `{"drop_next_writes": 2}` | The next 2 state-changing POSTs return their normal `200` but change nothing and don't store the key |
| `POST /chaos` `{"api_version": "v2"}` | Carrier only: switch to v2 (`"v1"` switches back) |
| `POST /chaos` `{"latency_ms": 300}` | Optional: delay every response |
| `GET /chaos` | Current chaos settings |
| `POST /reset` | Re-seed the data and clear all chaos. Run before every demo |

Chaos is deterministic on purpose ("the next 2 writes", not "20% of writes"), so the demo works the same way every time.

## 6. CallSpec and the repair contract

The agent keeps one **CallSpec** per action. Placeholders in `{braces}` are filled from the step.

```json
{
  "method": "POST",
  "path": "/intercepts",
  "body": {"tracking_no": "{target_id}", "recall_id": "{recall_id}", "reason": "{reason}"},
  "readback_path": "/shipments/{target_id}",
  "readback_field": "status",
  "readback_expect": ["intercept_requested"]
}
```

**LLM repair input:** the failing CallSpec, the HTTP status and body it got back, and `GET /api-spec`.
**LLM repair output:** one CallSpec as JSON, nothing else. The agent validates its shape before using it. The expected v2 answer:

```json
{"method": "POST", "path": "/shipments/{target_id}/return",
 "body": {"recall_id": "{recall_id}", "reason": "{reason}"},
 "readback_path": "/shipments/{target_id}", "readback_field": "status",
 "readback_expect": ["intercept_requested"]}
```

The list endpoint also changes (`tracking_no` → `shipment_ref`). Planning needs that only for recall 2, because recall 1 has already planned its steps by the time the API changes. Handle it with the same repair: accept either field name when reading the shipment list.

## 7. Seed data (synthetic)

### Recall 1: `RC-GB-2611`, lot `GB-2611`, granola bars, undeclared peanuts → 60 steps

| Seq | Action | Targets | Count |
|---|---|---|---|
| 1–20 | `hold_inventory` | `UNIT-0001` … `UNIT-0020` (all `available`) | 20 |
| 21–30 | `cancel_order` | `ORD-1001` … `ORD-1010` (`open`, customers `CUST-101` … `CUST-110`) | 10 |
| 31–38 | `intercept_shipment` | `TRK-5001` … `TRK-5008` (orders `ORD-1011` … `ORD-1018`, customers `CUST-111` … `CUST-118`) | 8 |
| 39–60 | `notify_customer` | `CUST-101` … `CUST-122` (every customer with an order of the lot; `ORD-1019` … `ORD-1022` are `delivered` to `CUST-119` … `CUST-122`) | 22 |

Decoy: `UNIT-0099`, lot `GB-2612`, returned by the fuzzy inventory search → denied by the policy gate, no step created.

### Recall 2: `RC-OB-3107`, lot `OB-3107`, oat bars, undeclared tree nuts → 24 steps (short, for the memory demo)

| Seq | Action | Targets | Count |
|---|---|---|---|
| 1–8 | `hold_inventory` | `UNIT-0101` … `UNIT-0108` | 8 |
| 9–12 | `cancel_order` | `ORD-2001` … `ORD-2004` (`CUST-201` … `CUST-204`) | 4 |
| 13–16 | `intercept_shipment` | `TRK-6001` … `TRK-6004` (orders `ORD-2005` … `ORD-2008`, `CUST-205` … `CUST-208`) | 4 |
| 17–24 | `notify_customer` | `CUST-201` … `CUST-208` | 8 |

Both recalls are seeded at every `POST /reset`.

### Background data (not in either recall)

So the agent has to find the recalled lot among normal traffic, the mocks also hold stock and orders that no recall touches. It is generated from fixed seeds in `mocks/seed.py`, so every reset gives the same data.

| System | Total | Of which in recall 1 | Variety |
|---|---|---|---|
| Warehouse | 272 units, 18 lots, 12 SKUs, 3 sites (`TOR-1`, `MIS-2`, `VAU-3`) | 20 units | 8 products; the recalled lot spans 2 pack sizes and 2 sites |
| Orders | 210 orders from 123 customers | 22 orders | Statuses `open`, `shipped`, `delivered`, `cancelled`; channels web, marketplace, wholesale; 12 cities; some repeat customers also bought a recalled lot |
| Carrier | 188 shipments | 8 in transit, 10 with labels printed, 4 delivered | `ground` and `express` |

Background lots never share the first 5 characters with a recalled lot, so the fuzzy warehouse search still returns exactly one decoy.

Extra fields on every record (additive; the fields above are unchanged): units have `product`, `site`, `expires`; orders have `sku`, `qty`, `channel`, `ship_to`, `placed_at`; shipments have `service`, `destination`.

`GET /stats` on each mock returns the size of its whole book (for example `{"units": 272, "lots": 18, "skus": 12, "sites": 3, "on_hold": 20}`), so the live log can show "20 of 272 units".

## 8. Atlas: database `tracer`

### `steps`
```json
{
  "_id": "ObjectId",
  "recall_id": "RC-GB-2611",
  "seq": 45,
  "system": "notify",
  "action": "notify_customer",
  "target_id": "CUST-118",
  "status": "verified",
  "idempotency_key": "RC-GB-2611:notify:CUST-118",
  "attempts": 1,
  "last_error": null,
  "proof": {"read_back_id": "NTF-0c41", "observed_status": "1 notification", "source": "read_back", "at": "ISODate"},
  "created_at": "ISODate",
  "updated_at": "ISODate"
}
```

| Field | Values |
|---|---|
| `system` | `warehouse` · `orders` · `carrier` · `notify` |
| `action` | `hold_inventory` · `cancel_order` · `intercept_shipment` · `notify_customer` |
| `status` | `planned` · `sent` · `verified` · `failed` |
| `proof.source` | `read_back` (normal) · `reconcile` (found done after a crash) |

Validator (create with `collMod`/`create_collection(validator=...)`):
```json
{"$jsonSchema": {
  "bsonType": "object",
  "required": ["recall_id", "seq", "system", "action", "target_id", "status", "idempotency_key", "attempts", "updated_at"],
  "properties": {
    "status": {"enum": ["planned", "sent", "verified", "failed"]},
    "system": {"enum": ["warehouse", "orders", "carrier", "notify"]},
    "action": {"enum": ["hold_inventory", "cancel_order", "intercept_shipment", "notify_customer"]},
    "attempts": {"bsonType": ["int", "long"], "minimum": 0},
    "proof": {"bsonType": "object", "required": ["read_back_id", "observed_status", "source", "at"]}
  },
  "anyOf": [
    {"properties": {"status": {"enum": ["planned", "sent", "failed"]}}},
    {"properties": {"status": {"enum": ["verified"]}}, "required": ["proof"]}
  ]
}}
```
Indexes: `{idempotency_key: 1}` **unique** · `{recall_id: 1, status: 1, seq: 1}`.

**Why the unique index matters:** planning is a bulk insert with `ordered=False`. If the agent crashes during planning and plans again, the duplicate keys are rejected, so the plan can never contain the same step twice.

### `audit_log`
```json
{"recall_id": "RC-GB-2611", "step_id": "ObjectId | null", "seq": 45, "actor": "tracer-agent:worker-1",
 "event": "verified", "policy_decision": "allow", "policy_reason": "lot GB-2611 in recall scope",
 "detail": {"attempt": 1}, "at": "ISODate"}
```
`event`: `planned` · `sent` · `verified` · `failed` · `denied` · `reconciled` · `repair_proposed` · `repair_verified` · `repair_rejected` · `memory_hit` · `paused` · `lease_claimed` · `duplicate_detected`.
Validator requires `recall_id`, `actor`, `event`, `at`. Index `{recall_id: 1, at: 1}`.
Status changes on `steps` and their audit entry are written **in one transaction**.

### `experiences`
```json
{"system": "carrier", "action": "intercept_shipment",
 "situation": "carrier intercept_shipment: POST /intercepts returned 404 Not Found",
 "fix": {"method": "POST", "path": "/shipments/{target_id}/return", "body": {"...": "..."},
         "readback_path": "/shipments/{target_id}", "readback_field": "status", "readback_expect": ["intercept_requested"]},
 "verified": true, "source_recall": "RC-GB-2611", "uses": 0, "created_at": "ISODate"}
```
- **Situation text is built by code, the same way every time:** `"{system} {action}: {method} {path} returned {status} {detail}"`. Consistent text gives consistent vector matches.
- Vector Search index `experience_situation` (Automated Embedding):
  ```json
  {"fields": [
    {"type": "autoEmbed", "modality": "text", "path": "situation", "model": "voyage-4"},
    {"type": "filter", "path": "system"},
    {"type": "filter", "path": "verified"}
  ]}
  ```
- Query:
  ```json
  {"$vectorSearch": {"index": "experience_situation", "path": "situation",
     "query": "<situation text>", "filter": {"system": "carrier", "verified": true},
     "numCandidates": 50, "limit": 3}},
  {"$project": {"fix": 1, "situation": 1, "score": {"$meta": "vectorSearchScore"}}}
  ```
  **Verified in the sandbox at 12:16:** `autoEmbed` works, and both `"query": "<text>"` and `"query": {"text": "<text>"}` are accepted. Measured scores: same failure 0.93, same failure with a different ID 0.90, same endpoint with a different error 0.86, different system 0.71. Threshold: **0.88**. A new fix becomes searchable about 5 s after it's saved. Implemented in `store/memory.py` (`situation_text`, `find_fix`, `save_fix`).
- TTL index `{created_at: 1}`, `expireAfterSeconds: 2592000` (30 days), so stale fixes age out.

### `control` (one document per recall)
```json
{"recall_id": "RC-GB-2611", "lot": "GB-2611", "status": "running | paused | done",
 "paused": false, "lease_owner": "worker-1", "lease_expires": "ISODate",
 "started_at": "ISODate", "finished_at": null, "report": null}
```
Index `{recall_id: 1}` **unique**. Lease claim:
```python
db.control.find_one_and_update(
    {"recall_id": rid, "$or": [{"lease_owner": None}, {"lease_owner": me}, {"lease_expires": {"$lt": now}}]},
    {"$set": {"lease_owner": me, "lease_expires": now + timedelta(seconds=15)}},
    return_document=ReturnDocument.AFTER)
# None → someone else holds the lease. Create the control doc first (upsert on recall_id only).
```

### Report (written to `control.report` when the recall finishes)
```json
{"recall_id": "RC-GB-2611", "lot": "GB-2611",
 "units_held": {"verified": 20, "total": 20}, "orders_cancelled": {"verified": 10, "total": 10},
 "shipments_intercepted": {"verified": 8, "total": 8}, "customers_notified": {"verified": 22, "total": 22},
 "duplicate_notifications": 0, "denied_targets": 1,
 "failed_attempts": 3, "llm_repairs": 1, "memory_hits": 0, "reconciled_after_crash": 1,
 "duration_s": 74, "generated_at": "ISODate"}
```
Counts come from an aggregation on `steps` and `audit_log`. `duplicate_notifications` comes from the notify mock (`GET /notifications?recall_id=`), because that's the real system.

## 9. Agent environment variables

| Var | Default | Purpose |
|---|---|---|
| `MONGODB_URI` | (required, `.env`) | Atlas |
| `LLM_API_KEY` | (required, `.env`) | Repair |
| `WORKER_ID` | `worker-1` | Lease owner. Same ID reclaims after a crash |
| `STEP_DELAY_MS` | `0` | Slows the loop so a demo audience can follow (use ~600) |
| `CRASH_AT_SEQ` | unset | Kill the process (`os._exit(137)`) right after this step's API call, before its read-back. Gives a deterministic half-done step for the demo |
| `MAX_ATTEMPTS` | `3` | Per step |
