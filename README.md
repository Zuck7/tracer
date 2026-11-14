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

- [ ] MongoDB collections with validators and indexes (`steps`, `audit_log`, `experiences`, `control`)
- [ ] Agent orchestrator with policy gate
- [ ] Mock warehouse, orders and carrier + notify services with chaos toggles
- [ ] Crash resume and read-back verification
- [ ] API-change repair with vector memory
