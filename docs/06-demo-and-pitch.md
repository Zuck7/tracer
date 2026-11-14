# 06 · Demo and pitch

## Demo order (and why)

Steps run in seq order: holds → cancels → **intercepts (31–38)** → **notifications (39–60)**. So the API change (carrier, intercepts) happens *before* the crash (notifications). The demo follows the same order:

1. Problem → 2. Silent failure → 3. API change + repair → 4. Crash + resume → 5. Memory (recall 2) → 6. Proof

## Demo setup (every time)

```bash
scripts/reset_demo.sh --wipe-memory       # clean mocks, clean Atlas (memory wiped for recall 1)
POST :8001/chaos {"drop_next_writes": 2}  # pre-armed, fires on the first holds
POST :8003/chaos {"api_version": "v2"}    # pre-armed, fires at the first intercept
STEP_DELAY_MS=600 CRASH_AT_SEQ=45 python -m agent.run --recall RC-GB-2611
```

In judging, a judge can press the buttons live instead of pre-arming. Pre-arming is for the video, so every take is identical.

## 1-minute submission video

| Time | Show | Say |
|---|---|---|
| 0:00–0:08 | Live log, recall starting, countdown | "Lot GB-2611 has undeclared peanuts. It's on shelves, in orders, on trucks and in homes. Tracer runs the recall and proves every step." |
| 0:08–0:20 | Step 1 goes `sent → failed → verified` | "The warehouse said 200 but didn't hold the unit. A 200 isn't proof. Tracer read it back, caught it and retried with the same key." |
| 0:20–0:33 | Intercept fails 404 → repaired → verified | "The carrier changed its API. Tracer asked the LLM for a fix, tested it on one shipment, and saved it to Atlas Vector Search only after it worked." |
| 0:33–0:48 | Process dies at #45 → restart → `reconciled` | "Now we kill it mid-notification. On restart it checks the real system, finds the notice was sent, and moves on. Every customer gets exactly one." |
| 0:48–0:60 | Recall 2: memory hit, 0 LLM calls; report from Atlas | "Second recall, same API change: fixed from memory, no LLM. Everything — state, audit, memory — lives in MongoDB Atlas." |

Record it with a screen recorder at 1080p. Cut dead time. Captions help if the room is noisy.

**What the resource guide requires of the video** (checked 1:30 PM):
- It must show **the code and the functionality built today**, not only the UI. Put 2 to 3 seconds of code on screen at each beat: the `$jsonSchema` validator in `store/setup_db.py`, `transition()` in `store/steps.py` (step + audit in one transaction), `reconcile()` in `agent/reconcile.py`, `find_fix()` in `store/memory.py`.
- It must have **working audio**. Record a voice-over and play the file back before uploading.
- The live log footer says "Built today"; keep it in frame.

## 3-minute finals demo

| Time | Beat | What happens |
|---|---|---|
| 0:00 | Problem | Lot GB-2611, undeclared peanuts. Start Tracer; point at the countdown to the next truck pickup |
| 0:25 | Silent failure | Judge presses **drop writes**. Warehouse says 200, no hold. Read-back catches it; retry with the same key |
| 0:55 | API change | Judge presses **change API**. Carrier renames `tracking_no` → `shipment_ref` and moves the endpoint. Agent repairs, tests on one shipment, saves the fix |
| 1:30 | Crash | Judge presses **kill agent** during notifications. Restart. One half-done step is reconciled; run `check_exactly_once` → 22 customers, 22 notices. *"Most agents would redo this step or skip it. Both are wrong. Watch it check the real system instead."* |
| 2:10 | Memory | Recall 2 (`RC-OB-3107`) with the same carrier change: memory hit, zero LLM calls. Show recall 1 vs recall 2 side by side from Atlas |
| 2:40 | Proof | Show the report in Atlas and one step's `proof`. Optional: set `paused: true`, agent stops within one step |

## Judge Q&A

| Question | Answer |
|---|---|
| What if it crashes after the call but before saving? | The step was saved as `sent` before the call. On restart we look it up in the real system by idempotency key, and only resend if the effect isn't there |
| How do you avoid notifying twice? | Reconcile before resending, idempotency keys, and a lease so only one worker runs a recall. The notify mock deliberately doesn't de-duplicate, so this is really our logic doing it |
| Why not trust the API's 200? | 200 means "received", not "done". We read the record back, and the database refuses a `verified` step without a `proof` |
| What if the LLM's repair is wrong? | It's tested on one step first and saved to memory only if the read-back passes. A bad guess costs one failed step, not the recall |
| How do you know memory helped? | Recall 2 makes zero LLM calls and has fewer failed attempts, measured in Atlas. Wipe `experiences` and run it again: the LLM call comes back |
| Why does the LLM not choose which customers to notify? | It's a safety process. Scope is plain code plus a policy gate. The LLM only repairs API calls, and even then it's verified |
| Why MongoDB? | State, audit, transactions, validation, leases, change streams, vector memory and embeddings all in one database. Nothing else to keep consistent |
| What's mocked? | The three business systems, so failures happen on cue. Atlas is real. Data is synthetic |
| How would this go to production? | Swap mocks for real WMS/OMS/carrier APIs behind the same CallSpec layer. The step engine doesn't change |

## Submission checklist (4:40 PM)

- [ ] `git log` and `git grep -i "mongodb+srv"` show no secrets. `.env` is not tracked
- [ ] Repo is **public**
- [ ] Root README: what it is, how to run it, **"Built today" list ticked and accurate**
- [ ] Video ≤ 60 seconds, shows code and functionality, **audio plays**, link works in a private window
- [ ] **Concise project description** pasted into the Cerebral Valley form (what we built today; use the first two paragraphs of the root README)
- [ ] All three team members added on the Cerebral Valley submission page
- [ ] Built in the provided Atlas Sandbox (required for finalists)

## Before round 1 judging (5:00–5:15)

- [ ] `scripts/reset_demo.sh`, mocks and live log running, agent ready to start
- [ ] Laptop on power, notifications off, font size up
- [ ] Backup video open in a browser tab
- [ ] Phone hotspot ready in case the Wi-Fi drops
- [ ] One person demos, one answers questions, one handles the laptop if something breaks
