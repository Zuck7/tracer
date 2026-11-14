"""The supplier's own records: products, lots, production dates, QA findings.

Collection `supplier_lots` in database `tracer`. This is Northfield Foods' master
data, so it lives in MongoDB with everything else Tracer knows. It is NOT the
stock, orders or shipments: those belong to the warehouse, order and carrier
systems, and Tracer only reaches them through their APIs.

The intake agent searches these records when a supplier says
"we have undeclared peanuts", to find which lot it is.

Seeded on first use and never wiped by a demo reset (master data, not run data).
All data is synthetic.
"""
import re
from typing import Any

from pymongo import ASCENDING
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from store.db import db, now

COLLECTION = db.supplier_lots
INDEX = "supplier_lot_search"
# Atlas Automated Embedding (Voyage) embeds search_text on insert and at query
# time, same as experiences: no embedding code here.
VECTOR_INDEX = {"fields": [{"type": "autoEmbed", "modality": "text", "path": "search_text",
                            "model": "voyage-4"}]}

SEED: list[dict[str, Any]] = [
    {"lot": "GB-2611", "supplier": "Northfield Foods", "product": "Oat & honey granola bars",
     "sku": "GRB-OAT-12", "produced": "2026-11-02", "line": "Line 3",
     "allergens_on_label": ["oats", "honey"],
     "qa_finding": "Lab test on Nov 13 found peanut protein. Peanuts are not on the label.",
     "recall_id": "RC-GB-2611"},
    {"lot": "GB-2612", "supplier": "Northfield Foods", "product": "Oat & honey granola bars",
     "sku": "GRB-OAT-12", "produced": "2026-11-03", "line": "Line 3",
     "allergens_on_label": ["oats", "honey"],
     "qa_finding": "Tested clean: no peanut protein.", "recall_id": None},
    {"lot": "OB-3107", "supplier": "Northfield Foods", "product": "Apple oat bars",
     "sku": "OBR-APL-08", "produced": "2026-11-05", "line": "Line 1",
     "allergens_on_label": ["oats"],
     "qa_finding": "Customer complaint on Nov 12; lab confirmed almond (tree nut) traces. "
                   "Tree nuts are not on the label.",
     "recall_id": "RC-OB-3107"},
    {"lot": "OB-3106", "supplier": "Northfield Foods", "product": "Apple oat bars",
     "sku": "OBR-APL-08", "produced": "2026-11-01", "line": "Line 1",
     "allergens_on_label": ["oats"], "qa_finding": "No issues.", "recall_id": None},
    {"lot": "TM-1150", "supplier": "Northfield Foods", "product": "Trail mix with peanuts",
     "sku": "TMX-PNT-10", "produced": "2026-11-04", "line": "Line 2",
     "allergens_on_label": ["peanuts", "tree nuts"],
     "qa_finding": "Contains peanuts, declared on the label. No issues.", "recall_id": None},
]


def _search_text(lot: dict[str, Any]) -> str:
    allergens = ", ".join(lot.get("allergens_on_label") or []) or "none"
    return (f"{lot['product']} lot {lot['lot']}, made {lot['produced']} on {lot['line']}. "
            f"QA: {lot['qa_finding']} Allergens on the label: {allergens}.")


def all_seed() -> list[dict[str, Any]]:
    """The two recalled lots and their decoys, plus ~650 lots from the shared world."""
    from mocks.world import lots as world_lots  # same generator as the mocks: they agree
    return SEED + world_lots()


def ensure_seeded() -> None:
    """Insert missing lots (never overwrites edits) and create the indexes. Cheap to re-run."""
    COLLECTION.create_index([("lot", ASCENDING)], unique=True, name="uniq_lot")
    have = set(COLLECTION.distinct("lot"))
    missing = [{**doc, "search_text": _search_text(doc), "created_at": now()}
               for doc in all_seed() if doc["lot"] not in have]
    if missing:
        COLLECTION.insert_many(missing, ordered=False)
    for doc in COLLECTION.find({"search_text": {"$exists": False}}):  # lots saved before search
        COLLECTION.update_one({"_id": doc["_id"]}, {"$set": {"search_text": _search_text(doc)}})
    try:
        if INDEX not in {ix["name"] for ix in COLLECTION.list_search_indexes()}:
            COLLECTION.create_search_index(SearchIndexModel(
                definition=VECTOR_INDEX, name=INDEX, type="vectorSearch"))
    except OperationFailure:
        pass  # search falls back to keywords below


_PROJECTION = {"_id": 0, "created_at": 0, "search_text": 0}


def list_lots() -> list[dict[str, Any]]:
    ensure_seeded()
    return list(COLLECTION.find({}, _PROJECTION).sort("lot", ASCENDING))


def count_lots() -> int:
    return COLLECTION.count_documents({})


def find_lot(lot: str) -> dict[str, Any] | None:
    return COLLECTION.find_one({"lot": lot}, _PROJECTION)


def search_lots(text: str, limit: int = 8) -> tuple[list[dict[str, Any]], str]:
    """The lots that best match what the supplier said, by meaning (Atlas Vector Search).
    Returns (lots, method). Falls back to a keyword search if the index isn't ready."""
    ensure_seeded()
    try:
        hits = list(COLLECTION.aggregate([
            {"$vectorSearch": {"index": INDEX, "path": "search_text", "query": {"text": text},
                               "numCandidates": 200, "limit": limit}},
            {"$project": {**_PROJECTION, "score": {"$meta": "vectorSearchScore"}}},
        ]))
        if hits:
            return hits, "vector"
    except OperationFailure:
        pass
    words = [w for w in re.findall(r"[a-z]{4,}", text.lower())
             if w not in {"have", "with", "there", "something", "wrong", "product", "products"}]
    query = {"$or": [{"search_text": {"$regex": re.escape(w), "$options": "i"}} for w in words]}
    hits = list(COLLECTION.find(query, _PROJECTION).limit(limit)) if words else []
    return hits, "keyword"


def open_issues() -> list[dict[str, Any]]:
    """Lots with an open QA problem (a defined recall), which QA would flag anyway."""
    return list(COLLECTION.find({"recall_id": {"$ne": None}}, _PROJECTION))
