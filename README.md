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

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
cp .env.example .env   # then fill in MONGODB_URI and LLM_API_KEY
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
