# Tracer: Product Recall Agent

Built at the MongoDB "Agents in Context" Toronto Hackathon (Nov 14, 2026), Track 3: Finish the Job. All code written during the event.

Tracer is an AI agent that runs a product recall end to end and proves every step worked. Given a contaminated lot, it puts the inventory on hold, cancels open orders, intercepts shipments in transit, notifies each affected customer exactly once, and writes a recall effectiveness report. It survives crashes, silent failures and API changes without missing or repeating a step, and it remembers fixes using MongoDB Atlas Vector Search.

All data is synthetic.

## How it works

- **Step lifecycle:** `planned → sent → verified / failed`. Only `verified` counts as done.
- **Write-ahead intent:** each step is saved as `sent` before the API call.
- **Read-back verification:** a 200 response is not proof, so the agent fetches the record and confirms the change.
- **Reconcile, don't replay:** after a crash, `sent` steps are checked against the real system by idempotency key.
- **Vector memory:** verified repairs are stored in Atlas and retrieved with Vector Search on the next recall.

Full design, schemas and demo script: [docs/](docs/README.md) (start with [architecture](docs/02-architecture.md) and [contracts](docs/03-contracts.md)).

## Stack

Python, FastAPI and MongoDB Atlas (state, audit log, vector memory, change streams). The only non-MongoDB service is the LLM.

## How it answers Track 3: Finish the Job

| The track asks | Tracer's answer | Where |
|---|---|---|
| A long multi-step task across several systems | One recall is 60 steps across a warehouse, an order system and a carrier with notifications, found among 272 units, 210 orders and 188 shipments | `agent/plan.py`, `mocks/` |
| MongoDB Atlas persists verified task state | Every step lives in `steps`; the database refuses a `verified` step without `proof`; each status change and its audit entry commit in one transaction | `store/setup_db.py`, `store/steps.py` |
| How does it tell an action that took effect from one that was only sent? | It reads the record back after every write. A 200 with no change is caught and retried with the same idempotency key | `agent/clients.py`, `agent/run.py` |
| How does it resume after a crash without replaying its history? | The step is saved as `sent` before the call. On restart only the `sent` steps are checked against the real system; done ones are not resent | `agent/reconcile.py` |
| How does it adapt when fields or workflow change? | Carrier renames a field and moves an endpoint: Claude proposes a new call, it is tested on one shipment, then used. A truck collects an order early: the agent intercepts the shipment instead of cancelling | `agent/repair.py`, `agent/run.py` |
| Vector Search retrieves relevant past experience | Proven fixes are stored in `experiences` with Atlas Automated Embedding. The next recall finds the fix by similarity and makes no LLM call | `store/memory.py` |

It also covers parts of the other two tracks: a policy gate checks every action before it runs and every action is audited with its actor (Track 1), and remembered fixes expire by TTL and can be retired when they stop working (Track 2).

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in MONGODB_URI and LLM_API_KEY
python -m scripts.check_setup   # checks .env, Atlas and keys without printing them
python -m store.setup_db        # collections, validators, indexes (safe to re-run)
scripts/run_demo.sh             # three mock systems + the live log
```

Open the live log URL it prints, press **Start agent**, then use the disruption buttons. A full walk-through with expected results is in [docs/07-full-test.md](docs/07-full-test.md).

```bash
python -m scripts.check_exactly_once RC-GB-2611   # one notice per customer, checked in the notify system
python -m scripts.compare_recalls                 # recall 1 vs recall 2: LLM calls and memory hits, from Atlas
python -m pytest -q tests                         # agent loop tests
```

## Built today

- [x] MongoDB collections with validators and indexes (`steps`, `audit_log`, `experiences`, `control`): `$jsonSchema` refuses a `verified` step without `proof`, unique idempotency keys, TTL on learned fixes
- [x] Atlas store (`store/`): step + audit entry written in one transaction, lease with `findOneAndUpdate`, pause flag, report
- [x] Vector memory (`store/memory.py`): Atlas Vector Search with Automated Embedding (Voyage `voyage-4`), score threshold measured in the sandbox; a fix that stops working can be retired and is kept as history
- [x] Live log server (`web/app.py`): MongoDB change stream → browser over server-sent events, agent start / kill, chaos proxy, pause flag, reset; phone page for judges at `/judge`
- [x] Agent orchestrator with policy gate (`agent/`): CallSpecs, write-ahead `sent` before every call, read-back verification with proof, same-key retries, crash reconcile, lease and pause checks
- [x] Agent loop tests against in-process fake systems (`tests/`): happy path, silent failure, crash after call, crash before call, lease, pause
- [x] Mock warehouse, orders and carrier + notify services with chaos toggles (`mocks/`): idempotency keys, drop-writes, carrier API v1 → v2; notifications deliberately don't de-duplicate. Synthetic data: 272 units, 210 orders and 188 shipments across 18 lots, of which one lot is recalled
- [x] Crash resume and read-back verification, proven on Atlas: dropped writes caught and retried; `kill -9` mid-recall → restart → exactly one notice per customer (`scripts/check_exactly_once.py`)
- [x] Setup check (`scripts/check_setup.py`): `.env` present and git-ignored, Atlas reachable, transactions supported, write access to `tracer`, API keys set; never prints a secret
- [x] API-change repair with vector memory (`agent/repair.py`): on a 4xx, Atlas Vector Search for a proven fix first, else Claude Opus 5.5 proposes a CallSpec (structured JSON output) from the error and the system's current API spec; a guard keeps the same target and the same proof; the fix is tested on one step and saved only after its read-back passes. Proven live: recall 1 repaired by Claude, recall 2 fixed from memory with no LLM call
- [x] Pause waits and resumes from the same step; the report lists steps that need a human
- [x] Scope with a reason per item (`agent/plan.py`): what was found in each system by status and what was decided (open order → cancel, shipped → intercept its shipment, delivered → notice only, label printed → skip), plus a cross-check that every shipped order is a shipment the carrier knows
- [x] Shipped-before-cancel escalation: if a truck collects an order before its cancel lands (409), the agent adds a step to intercept that shipment instead; mocks gained label-printed shipments and an early-pickup switch
- [x] Live log as a guided recall story (`web/`): six chapters, the per-system breakdown with reasons, disruption buttons in the chapter where they happen (warehouse glitch, truck arrives early, carrier API change, crash mid-step), plain-English log, proof and insights from Atlas, recall 1 vs recall 2 comparison
- [x] 12 agent loop tests: also LLM repair saved to memory, memory reuse with no LLM call, unsafe fix rejected, pause and resume, crash on demand, escalation, cross-check
