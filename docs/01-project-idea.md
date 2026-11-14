# 01 · Project idea

## The problem

When a food product is found to be unsafe, a company has hours to pull it out of circulation. The affected lot is spread across systems that don't talk to each other:

- units on **warehouse shelves**
- **open orders** that haven't shipped yet
- **shipments** already on trucks
- products already in **customers' homes**

A recall coordinator works through these one by one, usually from spreadsheets and email. The failure modes are exactly the ones agents also have:

| Failure | Real-world cost |
|---|---|
| A step is reported done but didn't take effect | Unsafe product still ships |
| The process stops halfway and restarts from scratch | Customers get two or three notices and stop trusting them |
| The process stops halfway and resumes in the wrong place | Some customers never hear about it |
| A system changed its fields or endpoints | The automation breaks silently or stops |

## What Tracer does

Given a recall (`RC-GB-2611`, lot `GB-2611`, granola bars, undeclared peanuts), Tracer:

1. **Holds** every unit of the lot in the warehouse (20 units)
2. **Cancels** open orders that contain the lot (10 orders)
3. **Intercepts** shipments already in transit (8 shipments)
4. **Notifies** each affected customer **exactly once** (22 customers)
5. Writes a **recall effectiveness report** from Atlas: units held, orders stopped, trucks intercepted, customers reached, duplicates (0)

That's 60 steps, each one proven by reading the real system back.

## Why it fits Track 3: Finish the Job

The track asks three questions. Each one maps to a feature we demo live.

| Track 3 question | Tracer's answer | Demo moment |
|---|---|---|
| How does it tell an action that **took effect** from one that was **only sent**? | **Read-back verification.** A step is `verified` only after the agent fetches the record and sees the change. The proof is stored on the step | Warehouse returns 200 but drops the hold; the agent catches it and retries |
| How does it **resume after a crash without replaying** its history? | **Write-ahead intent + reconcile.** The step is saved as `sent` before the call. On restart, each `sent` step is checked against the real system by idempotency key | `kill -9` during notifications; restart; every customer has exactly one notice |
| How does it **adapt** when a system's fields or workflow change? | **Repair + memory.** The LLM proposes a new call shape, it's tested on one step, and only a fix that passes read-back is saved to Atlas Vector Search | Carrier renames `tracking_no` → `shipment_ref`; recall 1 repairs it; recall 2 finds the fix in memory with no LLM call |

## Why we can win

Judging round 1: technical demo 35% · implementation difficulty 30% · impact 20% · creativity 15%.

- **Technical demo (35%):** every feature has a visible "press button → watch it recover" moment. Judges trigger the chaos themselves.
- **Difficulty (30%):** transactions, `$jsonSchema` validators, unique indexes, leases with `findOneAndUpdate`, change streams, Vector Search with Automated Embedding, TTL. Real distributed-systems problems, not a chat wrapper.
- **Impact (20%):** food safety. Easy to explain, high stakes, a countdown to the next truck pickup.
- **Creativity (15%):** not on the hackathon's example list. Zuhriddin works at a 3PL where lots and expiry dates are tracked daily, so the pitch is grounded.

## Non-goals (don't build these)

- A dashboard. The UI is a **live log** of steps. It is not the product. (Dashboards as the main feature are banned.)
- Chat with your recall data. (Basic RAG is banned.)
- Real integrations. The three systems are mocks so failures trigger on cue. **Atlas is real.**
- Real customer data. Everything is synthetic.
- Streamlit. Banned.

## Where the AI is

Judges will ask. Answer:

- **Repair:** when the carrier API changes, the LLM reads the failing request, the error and the carrier's current API description, and proposes a new call shape. That's the agentic core.
- **Memory:** verified fixes are embedded and retrieved with Atlas Vector Search, so the agent doesn't need the LLM for a problem it has already solved.
- **Wording (optional):** the LLM writes the customer notice and the report summary.
- **Deliberately not AI:** deciding *which* units, orders and customers are in scope is plain code plus a policy gate. A recall is safety-critical, so the LLM never chooses targets.
