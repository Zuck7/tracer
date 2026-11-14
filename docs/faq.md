# Design questions

**What if the agent crashes after the call but before saving the result?**
The step was saved as `sent` before the call went out. On restart Tracer looks the effect up in the real system by the step's idempotency key, and only resends if it isn't there. Nothing is replayed blindly and nothing is skipped.

**How does it avoid notifying a customer twice?**
Reconcile before resending, the same idempotency key on every attempt, and a lease so only one worker runs a recall. The notify service deliberately does not de-duplicate (like real SMS and email gateways), so the guarantee comes from Tracer's own logic, and `scripts/check_exactly_once.py` counts it in the notify system.

**Why not trust the API's 200?**
A 200 means "received", not "done". Tracer reads the record back after every write, and the `$jsonSchema` validator on `steps` rejects a `verified` step that has no `proof`, so even a bug in the agent can't record a success it didn't see.

**What if Claude's repair is wrong?**
It is tested on one step first and kept only if that step's read-back passes. A guard also requires the fix to keep the same target and the same proof check, so a fix can't "pass" by checking something easier. A bad guess costs one failed step, which is left for a person, not the recall.

**How do you know memory helped?**
Recall 2 hits the same carrier change and fixes it with zero LLM calls, measured in Atlas (`scripts/compare_recalls.py`). Wipe `experiences` and run it again, and the LLM call comes back.

**Why vector search and not an exact match on the error?**
Error text varies: different IDs, paths and wording for the same underlying change. Similar situations should find the same fix.

**Why doesn't the LLM decide which customers to notify?**
A recall is safety-critical. Scope is plain code with a reason recorded for every item, and a policy gate checks every action before it runs. The warehouse lot search is deliberately fuzzy and returns a unit from a neighbouring lot, which the gate denies and audits.

**Why MongoDB for all of it?**
Step state, audit trail, transactions, validation, leases, the pause flag, change streams, vector memory and embeddings all live in one database, so there is nothing else to keep consistent with it.

**What's mocked?**
The three business systems, so failures can be triggered on cue. Atlas and Claude are real. All data is synthetic.

**How would this go to production?**
Replace the mocks with real warehouse, order and carrier APIs behind the same CallSpec layer. The step engine, verification and memory don't change.
