# 04 · Roadmap

Event schedule: lunch 1:00 · **submissions due 5:00 PM** · round 1 judging 5:15–6:45 · dinner 6:00 · finals 7:00–8:00 · winners 8:15.

**Rules for the day**
- Each block ends at a **gate**. Move on only when the gate is true.
- **Push to `main` at every gate**, so there's always a working version to submit.
- If a gate slips more than 15 minutes, **cut scope, don't move the freeze**. Cut order: judge's phone page → pause flag → TTL expiry.
- **Never cut:** crash resume and read-back verification.
- **Feature freeze at 3:40.** Bug fixes only after that.

## Overview

| Time | Block | Gate |
|---|---|---|
| 11:40 – 12:00 | Contract freeze + setup | Everyone agrees on [03-contracts.md](03-contracts.md); each laptop connects to Atlas and can push |
| 12:00 – 1:00 | **Happy path** (12:30 integration check) | One command runs recall 1 and all 60 steps are `verified` with `proof` |
| 1:00 – 1:20 | Lunch + stand-up | Happy path green, or everyone fixes it first |
| 1:20 – 2:15 | **Silent failure + crash resume** | Drop-writes caught and retried; crash at seq 45 + restart → exactly 22 notifications |
| 2:15 – 3:15 | **API change repair + memory** | Recall 1 repairs the carrier via LLM and saves the fix; recall 2 uses memory, 0 LLM calls |
| 3:15 – 3:40 | Report, then stretch | Report written to `control.report` |
| 3:40 – 4:10 | Freeze; rehearse 5×; record a backup run | Two clean runs in a row |
| 4:10 – 4:40 | 1-minute video + README | Video ≤ 60 s, uploaded |
| 4:40 – 4:50 | **Submit** | Public repo, no secrets in history, all 3 on the submission page |
| 4:50 – 5:00 | Buffer | |

## 11:40 – 12:00 · Contract freeze + setup (all)

- [ ] Read [03-contracts.md](03-contracts.md) together. Change anything now; after 12:00 changes need all three to agree
- [ ] Each person has `.env` with `MONGODB_URI` and `LLM_API_KEY`
- [ ] Each person can run `python -c "from store.db import db; print(db.list_collection_names())"` (after B pushes `store/db.py`, ~11:50)
- [ ] Teammates added as repo collaborators; everyone can push
- [ ] `requirements.txt` pushed (B)

## 12:00 – 1:00 · Happy path

**A: Agent**
- [ ] `agent/clients.py`: one function per action that runs a CallSpec (call + read-back), with the `Idempotency-Key` header
- [ ] `agent/plan.py`: scope via the list endpoints, build the 60 steps in seq order, idempotency keys
- [ ] `agent/policy.py`: lot / action / system checks; denied targets → audit only
- [ ] `agent/run.py`: the loop from [02 Flow 1](02-architecture.md#flow-1-happy-path), with `STEP_DELAY_MS`
- [ ] Until B's store is ready, use a tiny in-memory stub with the same function names

**B: Atlas layer**
- [ ] `store/db.py`: client from `MONGODB_URI`, `db = client["tracer"]` (push by 11:50, everyone needs it)
- [ ] `store/setup_db.py`: creates the 4 collections, validators, normal indexes and the TTL index. Safe to re-run
- [ ] `store/steps.py`: `insert_plan`, `next_steps`, `mark_sent`, `mark_verified`, `mark_failed`, `steps_in_status`. Each status change + audit entry in **one transaction**
- [ ] **15-minute spike:** create the `autoEmbed` vector index on `experiences`, insert one doc, run one `$vectorSearch` with text. Report back: works, or switch to the Voyage API

**C: Systems + demo**
- [ ] `mocks/seed.py`: both recalls from [03 §7](03-contracts.md#7-seed-data-synthetic) + decoy unit
- [ ] `mocks/warehouse.py`, `mocks/orders.py`, `mocks/carrier.py`: list, write, read-back endpoints, key handling, `/reset`
- [ ] `/chaos` endpoints exist (they can be no-ops for now)
- [ ] `scripts/run_mocks.sh` starts all three

**12:30 integration check:** step 1 (`hold UNIT-0001`) goes `planned → sent → verified` using A's loop, B's store and C's warehouse mock. If it doesn't, all three fix it together before continuing.

## 1:20 – 2:15 · Silent failure + crash resume (never cut)

**A**
- [ ] Read-back mismatch → `mark_failed` with `last_error`; retry pass with the same key, up to `MAX_ATTEMPTS`
- [ ] `reconcile()` at startup: for each `sent` step, read back → verified (`source: reconcile`) / resend / `duplicate_detected`
- [ ] `CRASH_AT_SEQ`: `os._exit(137)` right after the API call

**B**
- [ ] `store/control.py`: create control doc, `claim_lease`, `renew_lease`, `is_paused`
- [ ] Query helper for steps stuck in `sent`
- [ ] `scripts/check_exactly_once.py`: compares notifications per customer (from the notify mock) with verified notify steps. Prints ✅ / ❌

**C**
- [ ] `drop_next_writes` chaos works on all three mocks
- [ ] `GET /notifications?idempotency_key=` and `?recall_id=`
- [ ] `web/app.py` on :8000: the live log page, streaming `steps` and `audit_log` (B's change stream helper, or poll every 500 ms as a fallback), countdown in the header

**Gate test (run it, don't assume):**
```bash
POST :8001/chaos {"drop_next_writes": 2}
CRASH_AT_SEQ=45 python -m agent.run --recall RC-GB-2611   # dies at 45
python -m agent.run --recall RC-GB-2611                   # resumes
python scripts/check_exactly_once.py RC-GB-2611           # 22 customers, 22 notifications
```

## 2:15 – 3:15 · API change repair + memory

**A**
- [ ] `agent/repair.py`: on a 4xx from a write: build the situation text → `store.memory.find_fix()` → if miss: `GET /api-spec` + LLM → CallSpec JSON → validate shape
- [ ] Apply the CallSpec to the current step only, read back; pass → save the fix + use it for the rest; fail → step `failed`, audit `repair_rejected`
- [ ] Planning accepts `tracking_no` or `shipment_ref` from the shipment list

**B**
- [ ] `store/memory.py`: `save_fix()` (only called after a passed read-back), `find_fix()` with `$vectorSearch` + score threshold, `uses` counter
- [ ] Vector index created by `setup_db.py`
- [ ] `store/report.py`: the aggregation for the report, including the recall 1 vs recall 2 comparison

**C**
- [ ] Carrier v1/v2 switch, `GET /api-spec` for both versions
- [ ] Chaos buttons and kill/restart buttons on the live log page (kill = `SIGKILL` to the agent's PID; restart = spawn `python -m agent.run`)
- [ ] `scripts/reset_demo.sh`: `POST /reset` on all mocks + drop `steps`, `audit_log`, `control` (**keep `experiences` unless running the "no memory" comparison**)

**Gate test:**
```bash
scripts/reset_demo.sh --wipe-memory
POST :8003/chaos {"api_version": "v2"}
python -m agent.run --recall RC-GB-2611   # 1 LLM repair, fix saved
python -m agent.run --recall RC-OB-3107   # memory hit, 0 LLM calls
```

## 3:15 – 3:40 · Report, then stretch (in this order)

1. [ ] Report written to `control.report` and printed at the end of a run (B)
2. [ ] Pause flag button on the live log (B + C)
3. [ ] Change stream instead of polling for pause and the live log (B)
4. [ ] Judge's phone page: the chaos buttons on a mobile-sized page, reachable on the venue Wi-Fi (C)
5. [ ] Countdown that matters: open orders auto-ship when the countdown hits zero, so cancel steps fail with `409` if the agent is too slow (C)

## 3:40 – 4:50 · Freeze, rehearse, record, submit

See [06-demo-and-pitch.md](06-demo-and-pitch.md) for the scripts and the submission checklist.

## Risks to watch

| Risk | Early sign | Plan B |
|---|---|---|
| `autoEmbed` not available in the sandbox | Spike fails by 12:30 | Voyage API for embeddings + standard `vector` index. Still all in Atlas |
| LLM repair is flaky | Wrong CallSpec more than 1 in 5 | Give it a stricter prompt with the expected JSON shape and the spec; retry once; validate the shape |
| Transactions slow the loop | Steps take > 300 ms | Fine for a demo with `STEP_DELAY_MS`. Don't optimise |
| Venue Wi-Fi drops | Atlas timeouts | Phone hotspot ready; backup video recorded by 4:10 |
| Merge conflicts | Two people editing the same file | Stay in your own folder; changes to `03-contracts.md` announced out loud |
