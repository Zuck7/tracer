# Tracer

**An AI agent that runs a product recall end to end and proves every step worked.**

A lot of granola bars turns out to contain undeclared peanuts. The bad lot is sitting on warehouse shelves, in open orders, on trucks and in customers' homes, spread across systems that don't talk to each other. Tracer finds every unit of that lot among thousands of others, then:

1. puts every unit **on hold** in the warehouse
2. **cancels** open orders before they ship
3. **intercepts** shipments already on the road
4. **notifies** each affected customer **exactly once**
5. writes a **recall report** where every line is backed by proof

The hard part isn't the happy path. It's what happens when things go wrong mid-recall: a system says "200 OK" but did nothing, the agent process is killed halfway through, or a carrier changes its API. In a recall, each of those means unsafe food still ships, or a customer is never told, or is told three times. Tracer is built so none of that can happen, and MongoDB Atlas is what makes that possible.

Built in one day at the MongoDB "Agents in Context" hackathon, Toronto, Nov 14 2026. All data is synthetic.

## How it stays correct

| Failure | What Tracer does |
|---|---|
| **A write is silently dropped.** The warehouse returns 200 but the unit isn't on hold | A 200 is not proof. After every write Tracer reads the record back and only marks the step `verified` when it sees the change. The proof is stored on the step, and the database itself rejects a `verified` step without one |
| **The agent is killed mid-run** | Every step is saved as `sent` *before* the call goes out. On restart Tracer doesn't replay or skip: it checks each `sent` step against the real system by its idempotency key, and only resends if the effect isn't there. The notify system deliberately doesn't de-duplicate, so this logic is the only thing keeping it to one notice per customer |
| **An API changes.** The carrier renames a field and moves an endpoint | Tracer gets a 404, gives Claude the failing call, the error and the carrier's current API description, and gets back a new call spec. It tests the fix on one shipment, reads it back, and only then uses it for the rest |
| **The same problem happens again** | A fix that passed read-back is saved to Atlas Vector Search. The next recall finds it by similarity and repairs itself with **no LLM call** |
| **The world changes under it.** A truck collects an order before its cancel lands | The cancel fails with "already shipped", so Tracer adds a step to intercept that shipment instead |
| **Two workers, or a human who needs to step in** | A lease in Atlas means only one worker runs a recall. A pause flag stops the agent within one step, and it resumes from the same step |

Every step moves through `planned → sent → verified / failed`, and only `verified` counts as done. Each status change and its audit entry are written in one transaction, so the audit trail can never disagree with the state.

The AI is used where judgment helps and kept away where it would be dangerous. Claude repairs broken API calls and reads the supplier's plain-language description of the problem. Deciding *which* units, orders and customers are in scope is plain code behind a policy gate, because a recall is safety-critical.

## Measured on a full run

Searched 4,842 cases, 15,030 orders and 13,250 shipments (5,547 customers) for the recalled lots, with every disruption triggered during recall 1.

| | Recall 1 `RC-GB-2611` | Recall 2 `RC-OB-3107` |
|---|---|---|
| Steps verified | 61 / 61 (60 planned + 1 escalation) | 24 / 24 |
| Dropped warehouse write | caught on read-back, verified on attempt 2 | |
| Truck left early | cancel refused, shipment intercepted instead | |
| Carrier API change | 404 → Claude's fix tested on one shipment, saved to memory | 404 → fix found in memory (score 0.93), **0 LLM calls** |
| Agent killed mid-notification | restart reconciled the step, notice not resent | |
| Every system's records match Atlas | ✅ | ✅ |
| Exactly one notice per customer | ✅ 22 / 22 | ✅ 8 / 8 |

These checks are scripts you can run yourself (below). They read the business systems directly, not Tracer's own records.

## What runs

```
                 write, then read back
 ┌──────────────┐ ───────────────────▶ ┌──────────────────────────┐
 │ Tracer agent │                      │ Warehouse                │
 │  scope, plan │ ───────────────────▶ │ Orders                   │
 │  policy gate │                      │ Carrier + notifications  │
 │  verify      │ ───────────────────▶ │  (mock services with     │
 │  repair      │                      │   failure switches)      │
 └──────┬───────┘                      └──────────────────────────┘
        │ state, audit, memory ▲ fixes, pause, lease
        ▼                      │
 ┌──────────────────────────────────────────────────────────────┐
 │ MongoDB Atlas, database `tracer`                             │
 │ steps · audit_log · experiences · control · supplier_lots    │
 └──────────────────────────────────────────────────────────────┘
```

- **Supplier console** (`/console`): the supplier describes the problem in plain language (typed or spoken), Tracer finds the lot in their records with vector search and proposes a plan, and nothing changes until the supplier approves it. Items that need a person are resolved here, and the report is read here.
- **Live log** (`/`): the recall as it happens, chapter by chapter, with buttons that break things on purpose: drop a warehouse write, send a truck early, change the carrier's API, crash the agent. Click any step to trace it from plan to proof: every write to Atlas in order, the exact call, the read-back, and each repair or restart.

The three business systems are mocks so failures can be triggered on cue. They keep their own state in their own processes, separate from Atlas, so reading them back proves something real.

## MongoDB Atlas does all of it

| Need | Atlas feature | Where |
|---|---|---|
| Durable step state and checkpoint | `steps` collection, unique index on the idempotency key | `store/setup_db.py`, `store/steps.py` |
| "Verified" can't be faked | `$jsonSchema` validator requires `proof` when `status = verified` | `store/setup_db.py` |
| State and audit never disagree | Multi-document transaction per step transition | `store/steps.py` |
| Only one worker per recall | Lease claimed with `findOneAndUpdate` | `store/agent_store.py` |
| Live view of the agent | Change stream on `audit_log` (every transition writes one, in commit order), streamed to the browser | `web/app.py` |
| Memory of proven fixes | Atlas Vector Search with Automated Embedding (Voyage AI) | `store/memory.py` |
| Old fixes expire | TTL index on `experiences` | `store/setup_db.py` |
| Find the lot from a plain-language report | Vector search over the supplier's lot records | `store/catalogue.py` |

There is no other database, queue or cache. Outside Atlas, the only services are Claude (Anthropic API) for repair and intake, and optionally ElevenLabs for voice in the supplier console.

## Run it

Needs Python 3.10+, a MongoDB Atlas cluster and an Anthropic API key.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in MONGODB_URI and LLM_API_KEY (ElevenLabs keys are optional)
python -m scripts.check_setup   # checks .env, Atlas and keys without printing them
python -m store.setup_db        # collections, validators, indexes (safe to re-run)
scripts/run_demo.sh             # three mock systems + the web app
```

Open the URL it prints. Start with `/console` to declare a recall, or go straight to `/` and press **Start agent**. A step-by-step walkthrough with every disruption and what you should see is in [docs/walkthrough.md](docs/walkthrough.md).

Check a finished recall:

```bash
python -m scripts.prove_recall RC-GB-2611          # each business system's own records vs the proof in Atlas
python -m scripts.check_exactly_once RC-GB-2611    # one notice per customer, counted in the notify system
python -m scripts.compare_recalls                  # recall 1 vs recall 2: LLM calls and memory hits
python -m pytest -q                                # 33 tests: agent loop (crash, silent failure, repair, memory), intake, voice
```

## Code map

| Folder | What's in it |
|---|---|
| `agent/` | The recall loop (`run.py`), scoping with a reason per item (`plan.py`), policy gate (`policy.py`), crash reconcile (`reconcile.py`), API repair (`repair.py`), plain-language intake (`intake.py`) |
| `store/` | Every Atlas read and write: steps and audit in one transaction, lease, pause, vector memory, supplier lot records |
| `mocks/` | Warehouse, orders and carrier + notify services with failure switches, and a synthetic world of ~650 lots and 15,000 orders |
| `web/` | Live log, supplier console, step trace, voice |
| `scripts/` | Setup check, reset, and the proof scripts above |
| `tests/` | Agent loop tests against in-process fake systems |

More detail: [architecture](docs/architecture.md) · [API and schema contracts](docs/contracts.md) · [walkthrough](docs/walkthrough.md) · [design questions](docs/faq.md)

## Built today

Everything here was written on Nov 14, 2026:

- [x] Atlas schema: `steps`, `audit_log`, `experiences`, `control` and `supplier_lots`, with `$jsonSchema` validators, unique idempotency keys and a TTL on learned fixes
- [x] Step store: step and audit entry in one transaction, lease with `findOneAndUpdate`, pause flag, report
- [x] Agent loop: write-ahead `sent` before every call, read-back verification with stored proof, same-key retries, crash reconcile, policy gate
- [x] Scoping with a reason for every item, plus a cross-check that every shipped order is a shipment the carrier knows
- [x] Shipped-before-cancel escalation to an intercept
- [x] API-change repair with Claude (structured output), tested on one step, saved to vector memory only after read-back passes; recall 2 reuses it with no LLM call
- [x] Vector memory and lot search with Atlas Automated Embedding
- [x] Three mock business systems with idempotency, dropped writes, carrier API v1 → v2, early pickup, and a large synthetic world
- [x] Supplier console with plain-language intake, approval before any change, and voice (ElevenLabs)
- [x] Live log over a change stream, with disruption buttons and a per-step trace from plan to proof
- [x] Proof scripts (`prove_recall`, `check_exactly_once`, `compare_recalls`) and 33 tests
