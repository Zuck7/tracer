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

Python, FastAPI and MongoDB Atlas: state, audit log, transactions, `$jsonSchema` validation, leases, change streams, TTL, and Vector Search with Automated Embedding (Voyage AI). Every byte of agent state lives in the `tracer` database; there is no other store.

Outside services: Claude (Anthropic API) repairs changed API calls and reads the supplier's plain-language report, and ElevenLabs gives the supplier console an optional voice. Neither one decides which units, orders or customers are in scope: that is plain code behind a policy gate, because a recall is safety-critical.

## How it answers Track 3: Finish the Job

| The track asks | Tracer's answer | Where |
|---|---|---|
| A long multi-step task across several systems | One recall is 60 steps across three separate services: a warehouse, an order system and a carrier with customer notifications. The recalled lot is found among ~4,800 cases, ~15,000 orders and ~13,000 shipments of other lots, and nothing outside it is touched | `agent/plan.py`, `mocks/` |
| MongoDB Atlas persists verified task state | Every step lives in `steps`; the database refuses a `verified` step without `proof`; each status change and its audit entry commit in one transaction | `store/setup_db.py`, `store/steps.py` |
| How does it tell an action that took effect from one that was only sent? | It reads the record back after every write. A 200 with no change is caught and retried with the same idempotency key | `agent/clients.py`, `agent/run.py` |
| How does it resume after a crash without replaying its history? | The step is saved as `sent` before the call. On restart only the `sent` steps are checked against the real system; verified ones are skipped and nothing is resent | `agent/reconcile.py` |
| How does it adapt when fields or workflow change? | The carrier renames a field and moves an endpoint: Claude proposes a new call, it is tested on one shipment, then used. A truck collects an order before its cancel lands: the agent intercepts the shipment instead | `agent/repair.py`, `agent/run.py` |
| Vector Search retrieves relevant past experience | Proven fixes are stored in `experiences` with Atlas Automated Embedding. The next recall finds the fix by similarity and makes no LLM call. The supplier's lot records are searched the same way to find the lot from a plain-language report | `store/memory.py`, `store/catalogue.py` |

It also covers parts of the other two tracks: a supplier must approve the plan before anything changes, a policy gate checks every action before it runs, and every action is audited with its actor (Track 1); remembered fixes expire by TTL (Track 2).

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in MONGODB_URI and LLM_API_KEY (ElevenLabs keys are optional)
python -m scripts.check_setup   # checks .env, Atlas and keys without printing them
python -m store.setup_db        # collections, validators, indexes (safe to re-run)
scripts/run_demo.sh             # three mock systems + the live log
```

Open the URL it prints. `/console` is the supplier's side: say what happened, approve the plan, read the report. `/` is the live log, with the disruption buttons; click any step there to trace it from plan to proof. A full walk-through with expected results is in [docs/07-full-test.md](docs/07-full-test.md).

```bash
python -m scripts.prove_recall RC-GB-2611          # every system's own records vs what Tracer wrote in Atlas
python -m scripts.check_exactly_once RC-GB-2611    # one notice per customer, checked in the notify system
python -m scripts.compare_recalls                  # recall 1 vs recall 2: LLM calls and memory hits, from Atlas
python -m pytest -q                                # 33 tests: agent loop (crash, silent failure, repair, memory), intake, voice; needs .env
```

## Built today

- [x] MongoDB collections with validators and indexes (`steps`, `audit_log`, `experiences`, `control`): `$jsonSchema` refuses a `verified` step without `proof`, unique idempotency keys, TTL on learned fixes
- [x] Atlas store (`store/`): step + audit entry written in one transaction, lease with `findOneAndUpdate`, pause flag, report
- [x] Vector memory (`store/memory.py`): Atlas Vector Search with Automated Embedding (Voyage `voyage-4`), score threshold measured in the sandbox
- [x] Live log server (`web/app.py`): MongoDB change stream → browser over server-sent events, agent start / kill, chaos proxy, pause flag, reset; phone page for judges at `/judge`
- [x] Agent orchestrator with policy gate (`agent/`): CallSpecs, write-ahead `sent` before every call, read-back verification with proof, same-key retries, crash reconcile, lease and pause checks
- [x] Agent loop tests against in-process fake systems (`tests/`): happy path, silent failure, crash after call, crash before call, lease, pause
- [x] Mock warehouse, orders and carrier + notify services with chaos toggles (`mocks/`): idempotency keys, drop-writes, carrier API v1 → v2; notifications deliberately don't de-duplicate
- [x] Crash resume and read-back verification, proven on Atlas: dropped writes caught and retried; `kill -9` mid-recall → restart → exactly one notice per customer (`scripts/check_exactly_once.py`)
- [x] Setup check (`scripts/check_setup.py`): `.env` present and git-ignored, Atlas reachable, transactions supported, write access to `tracer`, API keys set; never prints a secret
- [x] API-change repair with vector memory (`agent/repair.py`): on a 4xx, Atlas Vector Search for a proven fix first, else Claude Opus 5.5 proposes a CallSpec (structured JSON output) from the error and the system's current API spec; a guard keeps the same target and the same proof; the fix is tested on one step and saved only after its read-back passes. Proven live: recall 1 repaired by Claude, recall 2 fixed from memory with no LLM call
- [x] Pause waits and resumes from the same step; the report lists steps that need a human
- [x] Scope with a reason per item (`agent/plan.py`): what was found in each system by status and what was decided (open order → cancel, shipped → intercept its shipment, delivered → notice only, label printed → skip), plus a cross-check that every shipped order is a shipment the carrier knows
- [x] Shipped-before-cancel escalation: if a truck collects an order before its cancel lands (409), the agent adds a step to intercept that shipment instead; mocks gained label-printed shipments and an early-pickup switch
- [x] Live log as a guided recall story (`web/`): six chapters, the per-system breakdown with reasons, disruption buttons in the chapter where they happen (warehouse glitch, truck arrives early, carrier API change, crash mid-step), plain-English log, proof and insights from Atlas, recall 1 vs recall 2 comparison
- [x] 12 agent loop tests: also LLM repair saved to memory, memory reuse with no LLM call, unsafe fix rejected, pause and resume, crash on demand, escalation, cross-check
- [x] Client console (`/console`, `web/console.html`): the supplier directs the agent: declare a recall in plain language, approve the plan, resolve items left for a human, read the report. No charts
- [x] Intake agent (`agent/intake.py`): text in / JSON out (`interpret`, `explain`) so a voice agent can call the same functions; Claude reads the message and searches the supplier's lot records, code decides; nothing changes until the supplier approves
- [x] Supplier lot records in Atlas (`store/catalogue.py`, `supplier_lots`): products, lots, QA findings, searched with Automated Embedding vector search
- [x] Large synthetic world (`mocks/world.py`): ~650 lots, ~5,000 cases, 15,000 orders, 6,000 customers, ~13,000 shipments, so a recall is a needle in a haystack; `/stats` on each mock
- [x] Recall proof script (`scripts/prove_recall.py`): read-only check that every business system agrees with what Tracer wrote in Atlas; unfinished runs keep their report as `needs_attention`
- [x] Step trace on the live log (`/trace` in `web/app.py`, `web/index.html`): click any step to see every write to Atlas in order, the call rebuilt from the CallSpec in force, the read-back and its proof, each repair and restart, with the reason for each; a list of the steps that hit a problem, tagged from the audit log; and a read-only check of the business system's record against the proof in Atlas
- [x] Voice in the supplier console (`web/voice.py`, `web/voice.js`): ElevenLabs voice agent that speaks through the same chat as typing, plus a narrator that reads Tracer's updates aloud
