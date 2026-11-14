# Tracer: Team Context

MongoDB "Agents in Context" Toronto Hackathon, Sat Nov 14, 2026
**Track 3: Finish the Job**, built **only on MongoDB Atlas**

> Read this first. It's the summary. The detailed build book is in [docs/](docs/README.md), and the API shapes and schemas everyone codes against are in [docs/03-contracts.md](docs/03-contracts.md).

---

## 1. The idea in 30 seconds

**Tracer is an AI agent that runs a product recall end to end and proves every step worked.**

Scenario (all synthetic data): lot **GB-2611** of granola bars contains undeclared peanuts. That lot is on warehouse shelves, in open orders, on trucks and in customers' homes. Tracer:

1. Puts every unit of the lot **on hold** in the warehouse
2. **Cancels open orders** containing the lot before they ship
3. **Intercepts shipments** already on the road
4. **Notifies each affected customer exactly once**
5. Writes a **recall effectiveness report**: units held, orders stopped, trucks intercepted, customers reached

The hard part, and the reason we win: it **survives crashes, silent failures and API changes** without missing or repeating anything, and it **remembers fixes** in MongoDB Vector Search.

---

## 2. Hackathon rules that affect us

| Rule | What it means for us |
|---|---|
| Must build in the provided Atlas Sandbox | Everything lives in Zuhriddin's sandbox project, `Cluster0`, database `tracer` |
| Repo must be public | **Never commit `.env` or the connection string.** Add `.env` to `.gitignore` before the first commit |
| New work only | All code written today after 10:45. README must list what we built |
| Banned: basic RAG, Streamlit, dashboard-as-main-feature | Our UI is just a live log, never the product |
| Submission | 1-minute demo video + public repo, due **5:00 PM** |

**Judging (round 1):** Technical demo 35% · Implementation difficulty 30% · Impact 20% · Creativity 15%

---

## 3. Tech stack (MongoDB only)

- **Language:** Python + FastAPI (adjust if we agreed otherwise)
- **Database, state, audit, memory:** MongoDB Atlas
- **Vector database:** Atlas Vector Search (no Pinecone, Redis, Postgres, etc.)
- **Embeddings:** Atlas Automated Embedding (Voyage AI, owned by MongoDB). Fallback: call the Voyage API ourselves and store the vectors in Atlas
- **LLM:** the only non-MongoDB service, used for planning and repairing changed APIs
- **Mock systems:** 3 small FastAPI services with chaos toggles

### Setup

1. Get the connection string from Zuhriddin **by direct message**
2. Create `.env` in the project root:
   ```
   MONGODB_URI=mongodb+srv://tracer-app:<password>@cluster0.xxxxx.mongodb.net/tracer?retryWrites=true&w=majority
   LLM_API_KEY=...
   ```
3. Confirm `.env` is in `.gitignore`
4. Optional: add the MongoDB MCP server to Claude Code (`npx -y mongodb-mcp-server@latest setup`, answer **No** to read-only)

---

## 4. Architecture

```
                 write, then read back
 ┌──────────────┐ ───────────────────▶ ┌──────────────────────────┐
 │ Tracer agent │                      │ Warehouse API (mock)     │
 │ - plans (LLM)│ ───────────────────▶ │ Order system (mock)      │
 │ - policy gate│                      │ Carrier + notify (mock)  │
 │ - verifies   │ ───────────────────▶ │  chaos: drop writes,     │
 │ - repairs    │                      │         change API       │
 └──────┬───────┘                      └──────────────────────────┘
        │ checkpoint + audit   ▲ fixes + pause signal
        ▼                      │
 ┌──────────────────────────────────────────────────────────────┐
 │ MongoDB Atlas, database: tracer                              │
 │  steps  │  audit_log  │  experiences (vector)  │  control    │
 └──────────────────────────────────────────────────────────────┘
```

### Step lifecycle (the core idea)

```
planned ──saved first──▶ sent ──API call──▶ [read-back check] ──match──▶ verified (done)
                          ▲                        │
                          └──retry, same key── failed ◀── missing or wrong
crash restart ──steps stuck in "sent"──▶ [read-back check]
```

- **A 200 response is not proof.** A step is `verified` only after the agent reads the record back (unit on hold, order cancelled, intercept accepted, exactly one notification).
- **Write-ahead:** save the step as `sent` **before** calling the API.
- **Reconcile, don't replay:** after a crash, check every `sent` step against the real system instead of redoing or skipping it.
- **Idempotency key** on every call, so a retry can never notify a customer twice.

---

## 5. MongoDB collections (database `tracer`)

### `steps`: task state and checkpoint
```json
{
  "_id": "...",
  "recall_id": "RC-GB-2611",
  "seq": 37,
  "system": "warehouse | orders | carrier | notify",
  "action": "hold_inventory | cancel_order | intercept_shipment | notify_customer",
  "target_id": "UNIT-0042",
  "status": "planned | sent | verified | failed",
  "idempotency_key": "RC-GB-2611:notify:CUST-118",
  "attempts": 1,
  "proof": { "read_back_id": "...", "observed_status": "hold", "at": "ISODate" },
  "updated_at": "ISODate"
}
```
- `$jsonSchema` validator: require `recall_id`, `status`, `idempotency_key`; require `proof` when `status = verified`
- Unique index on `idempotency_key`

### `audit_log`: who, what, why
```json
{ "recall_id": "...", "step_id": "...", "actor": "tracer-agent",
  "policy_decision": "allow | deny", "policy_reason": "lot in recall scope",
  "event": "sent | verified | failed | denied", "at": "ISODate" }
```
- Written **in the same transaction** as the `steps` update

### `experiences`: vector memory
```json
{ "system": "carrier", "situation": "intercept POST returned 404; field tracking_no missing",
  "fix": { "endpoint": "POST /shipments/{id}/return", "field_map": { "tracking_no": "shipment_ref" } },
  "verified": true, "created_at": "ISODate" }
```
- Atlas Vector Search index on `situation` (Automated Embedding)
- TTL index on `created_at` so stale fixes expire
- Only save a fix **after** it passed a read-back test

### `control`: pause flag
```json
{ "recall_id": "RC-GB-2611", "paused": false, "lease_owner": "worker-1", "lease_expires": "ISODate" }
```
- Agent watches it with a **change stream** and stops within one step when `paused: true`
- Lease claimed with `findOneAndUpdate`, so only one worker runs the recall

---

## 6. Who builds what

| Role | Owns | Done when |
|---|---|---|
| **A: Agent** | Orchestrator loop, LLM planning and scoping, policy gate, field-mapping repair | Full recall completes on the happy path |
| **B: Atlas layer** | Collections, validators, indexes, transactions, leases, resume logic, change streams, vector memory | Kill and restart resumes with zero repeat notifications |
| **C: Systems + demo** | 3 FastAPI mocks (warehouse, orders, carrier + notify) with chaos toggles, synthetic data, live timeline with countdown, judge's phone page, video | All 5 demo moments trigger on cue |

**First (by 11:40):** freeze the `steps` schema above and write down the mock API shapes. Then build in parallel, following the timeline below.

---

## 7. Today's timeline (revised 11:25 AM)

The detailed schedule, per-person checklists and gate tests are in **[docs/04-roadmap.md](docs/04-roadmap.md)**. Summary:

| Time | Block |
|---|---|
| 11:40 – 12:00 | Contract freeze ([docs/03-contracts.md](docs/03-contracts.md)) + setup |
| 12:00 – 1:00 | Happy path (12:30 integration check) |
| 1:00 – 1:20 | Lunch + stand-up |
| 1:20 – 2:15 | Silent failure + crash resume |
| 2:15 – 3:15 | API change repair + memory |
| 3:15 – 3:40 | Report, then stretch |
| **3:40** | **Feature freeze** |
| 3:40 – 4:40 | Rehearse 5×, backup recording, 1-minute video, README |
| 4:40 – 4:50 | **Submit** |
| 5:15 – 6:45 | Round 1 judging · 7:00 – 8:00 finals |

**If a gate slips by more than 15 minutes, cut scope instead of moving the freeze.** Cut in this order: judge's phone page → pause flag → TTL expiry.
**Never cut:** crash resume and read-back verification. They are the project.

---

## 8. Demo script (3 minutes)

| Time | Beat | What happens |
|---|---|---|
| 0:00 | Problem | "Lot GB-2611 has undeclared peanuts." Tracer starts; countdown to next truck pickup |
| 0:25 | Silent failure | Judge presses **drop writes**: warehouse says 200 but no hold. Agent catches it on read-back, retries |
| 0:55 | API change | Judge presses **change API**: carrier renames `tracking_no` to `shipment_ref`. Agent repairs, tests, saves the fix |
| 1:30 | Crash | Judge presses **kill agent** during notifications (step ~45). Restart: reconciles one half-done step, continues. Every customer has exactly one notice |
| 2:10 | Memory | Second recall with the same change: fixed first try. Retry counts side by side from Atlas |
| 2:40 | Proof | Query Atlas for the recall effectiveness report, each step with its proof |

**One sentence to say during the crash:** "Most agents would redo this step or skip it. Both are wrong. Watch it check the real system instead."

---

## 9. Judge Q&A cheat sheet

- **Crash after the call but before saving?** Step was saved as `sent` first; on restart we look it up by idempotency key.
- **Avoid notifying twice?** Idempotency keys + a lease so only one worker runs.
- **Why not trust the API?** 200 means received, not done. We read the record back.
- **LLM repair wrong?** Tested on one step first; only saved if the read-back passes.
- **Proof memory helped?** Retry count drops on the second run, measured in Atlas.
- **Why MongoDB?** State, audit, vector memory, embeddings and the pause flag all in one database.
- **What's mocked?** The four business systems, so we can trigger failures on cue. Atlas is real. Data is synthetic.

---

Full planning doc: https://claude.ai/artifact/5ae4XTA1ANnCpmvvef5uns
