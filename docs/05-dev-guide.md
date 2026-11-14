# 05 · Dev guide

## Repo layout

```
tracer/
├── agent/              A · the recall agent
│   ├── run.py          entry point: python -m agent.run --recall RC-GB-2611
│   ├── plan.py         scope the lot, build steps
│   ├── policy.py       policy gate
│   ├── clients.py      run a CallSpec: call + read-back
│   ├── callspecs.py    default CallSpec per action (v1)
│   ├── reconcile.py    resume after a crash
│   └── repair.py       LLM repair + memory lookup
├── store/              B · every MongoDB read/write
│   ├── db.py           client + db = client["tracer"]
│   ├── setup_db.py     collections, validators, indexes (safe to re-run)
│   ├── steps.py        step transitions + audit, in transactions
│   ├── control.py      lease, pause, report
│   ├── memory.py       experiences + $vectorSearch
│   └── report.py       effectiveness report aggregation
├── mocks/              C · fake business systems
│   ├── seed.py
│   ├── common.py       idempotency + chaos helpers shared by all mocks
│   ├── warehouse.py    :8001
│   ├── orders.py       :8002
│   └── carrier.py      :8003 (shipments + notifications)
├── web/                C · live log page, :8000
├── scripts/            reset_demo.sh, run_mocks.sh, check_exactly_once.py
├── docs/
└── requirements.txt
```

**Stay in your own folder.** That alone prevents almost every merge conflict. The only shared surface is the function names in `store/` and the HTTP contract in [03](03-contracts.md).

## Setup (each laptop)

```bash
git clone https://github.com/Zuck7/tracer.git && cd tracer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # paste MONGODB_URI (from Zuhriddin by DM) and LLM_API_KEY
python -m store.setup_db    # safe to re-run
```

`requirements.txt` (starting point):
```
fastapi
uvicorn[standard]
pymongo
python-dotenv
httpx
anthropic
```
(Swap `anthropic` for whichever LLM SDK matches the team's key.)

## Running everything

```bash
# terminal 1: the three mocks
uvicorn mocks.warehouse:app --port 8001 &
uvicorn mocks.orders:app    --port 8002 &
uvicorn mocks.carrier:app   --port 8003 &

# terminal 2: the live log
uvicorn web.app:app --port 8000

# terminal 3: the agent
python -m agent.run --recall RC-GB-2611
```

Reset before every demo run: `scripts/reset_demo.sh` (re-seeds mocks, clears `steps`, `audit_log`, `control`).

## Code rules

- **All MongoDB access goes through `store/`.** The agent calls `store.steps.mark_sent(step)`, never `db.steps.update_one(...)`. One place to get transactions right.
- **Always name the database:** `client["tracer"]`. The MCP server's connection string has no default database.
- **`mark_sent` before the HTTP call. Always.** If you're writing an HTTP call and the step isn't `sent` yet, stop.
- **Never mark `verified` from a status code.** Only from a read-back.
- **Never invent a new idempotency key on retry.** Read it from the step.
- **Times in UTC:** `datetime.now(timezone.utc)`.
- **Transactions in pymongo:**
  ```python
  with client.start_session() as s:
      s.with_transaction(lambda s: (
          db.steps.update_one({"_id": sid}, {"$set": {...}, "$inc": {"attempts": 1}}, session=s),
          db.audit_log.insert_one({...}, session=s),
      ))
  ```
- **No secrets in code, docs, commits or screenshots.** `.env` only.

## Git workflow

- Work on `main`, commit small and often, folder by folder.
- `git pull --rebase` before every push.
- Push at every gate in the [roadmap](04-roadmap.md).
- Before pushing, check `git status` doesn't show `.env`.
- Commit messages say what was built: `store: lease claim and renew`. The judges read the history to confirm everything was built today.

## Useful MongoDB checks (mongosh, Atlas Data Explorer, or ask Claude via the MCP server)

```js
use tracer
db.steps.aggregate([{$match: {recall_id: "RC-GB-2611"}}, {$group: {_id: "$status", n: {$sum: 1}}}])
db.steps.find({status: "sent"})                          // anything half-done?
db.audit_log.find({recall_id: "RC-GB-2611"}).sort({at: -1}).limit(20)
db.experiences.find({}, {situation: 1, uses: 1})
db.control.findOne({recall_id: "RC-GB-2611"}).report
```

## Starter prompts for Claude Code

Paste the one for your role into Claude Code at the start of the 12:00 block. Each one points Claude at the contract, so the three parts fit together.

**A: Agent**
> Read docs/02-architecture.md and docs/03-contracts.md. Build `agent/` as described in docs/05-dev-guide.md: CallSpecs for the 4 actions (v1), a client that runs a CallSpec with an Idempotency-Key header and then reads back, planning from the mock list endpoints with the policy gate, and the main loop from Flow 1. Use the `store/` function names from docs/04-roadmap.md; if `store/` isn't there yet, write a small in-memory stub with the same names. Don't write any MongoDB code in `agent/`.

**B: Atlas layer**
> Read docs/02-architecture.md and docs/03-contracts.md §8. Build `store/`: `db.py` reading MONGODB_URI from .env with database `tracer`; `setup_db.py` that creates the 4 collections with the exact validators and indexes in §8 and is safe to re-run; `steps.py` with insert_plan (bulk, ordered=False, ignore duplicate keys), mark_sent, mark_verified, mark_failed, each writing the step update and its audit_log entry in one transaction. Then use the MongoDB MCP server to confirm the collections and indexes exist in database `tracer`.

**C: Systems + demo**
> Read docs/03-contracts.md §1–5 and §7. Build the three FastAPI mocks in `mocks/` with in-memory state seeded from `mocks/seed.py`, exactly the endpoints and JSON shapes in the contract, Idempotency-Key handling (notifications deliberately do NOT de-duplicate), `/chaos` with `drop_next_writes` and the carrier `api_version` v1/v2 switch, `/api-spec`, and `/reset`. Add CORS for all origins. Then write `scripts/run_mocks.sh` and test each endpoint with curl.
