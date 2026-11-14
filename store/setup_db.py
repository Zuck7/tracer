"""Create the `tracer` collections, validators and indexes. Safe to re-run.

    python -m store.setup_db

Shapes follow docs/03-contracts.md section 8.
"""
from pymongo import ASCENDING
from pymongo.errors import CollectionInvalid, OperationFailure
from pymongo.operations import SearchIndexModel

from store.db import db

STEP_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["recall_id", "seq", "system", "action", "target_id", "status",
                     "idempotency_key", "attempts", "updated_at"],
        "properties": {
            "status": {"enum": ["planned", "sent", "verified", "failed"]},
            "system": {"enum": ["warehouse", "orders", "carrier", "notify"]},
            "action": {"enum": ["hold_inventory", "cancel_order", "intercept_shipment", "notify_customer"]},
            "attempts": {"bsonType": ["int", "long"], "minimum": 0},
            "proof": {"bsonType": "object", "required": ["read_back_id", "observed_status", "source", "at"]},
        },
        # A "verified" step must carry proof. The database itself refuses fake success.
        "anyOf": [
            {"properties": {"status": {"enum": ["planned", "sent", "failed"]}}},
            {"properties": {"status": {"enum": ["verified"]}}, "required": ["proof"]},
        ],
    }
}

AUDIT_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["recall_id", "actor", "event", "at"],
        "properties": {
            "event": {"enum": [
                "planned", "sent", "verified", "failed", "denied", "reconciled",
                "repair_proposed", "repair_verified", "repair_rejected", "memory_hit",
                "paused", "lease_claimed", "duplicate_detected",
            ]},
        },
    }
}

VALIDATORS = {"steps": STEP_VALIDATOR, "audit_log": AUDIT_VALIDATOR, "experiences": None, "control": None}

VECTOR_INDEX_NAME = "experience_situation"
VECTOR_INDEX = {
    "fields": [
        {"type": "autoEmbed", "modality": "text", "path": "situation", "model": "voyage-4"},
        {"type": "filter", "path": "system"},
        {"type": "filter", "path": "verified"},
    ]
}


def ensure_collection(name: str, validator: dict | None) -> None:
    """Create the collection, or update its validator if it already exists."""
    try:
        if validator:
            db.create_collection(name, validator=validator, validationLevel="strict")
        else:
            db.create_collection(name)
        print(f"  created  {name}")
    except CollectionInvalid:
        if validator:
            db.command("collMod", name, validator=validator, validationLevel="strict")
        print(f"  exists   {name} (validator refreshed)" if validator else f"  exists   {name}")


def ensure_indexes() -> None:
    # Unique key: planning twice (e.g. after a crash mid-plan) can never duplicate a step.
    db.steps.create_index([("idempotency_key", ASCENDING)], unique=True, name="uniq_idempotency_key")
    # The agent's main query: "next steps for this recall in this status, in order".
    db.steps.create_index([("recall_id", ASCENDING), ("status", ASCENDING), ("seq", ASCENDING)],
                          name="recall_status_seq")
    db.audit_log.create_index([("recall_id", ASCENDING), ("at", ASCENDING)], name="recall_at")
    # One control document per recall; the lease lives on it.
    db.control.create_index([("recall_id", ASCENDING)], unique=True, name="uniq_recall_id")
    # TTL: stale fixes age out after 30 days.
    db.experiences.create_index([("created_at", ASCENDING)], expireAfterSeconds=30 * 24 * 3600,
                                name="ttl_created_at")
    print("  indexes  ok")


def ensure_vector_index() -> None:
    existing = {ix["name"] for ix in db.experiences.list_search_indexes()}
    if VECTOR_INDEX_NAME in existing:
        print(f"  exists   vector index {VECTOR_INDEX_NAME}")
        return
    try:
        db.experiences.create_search_index(
            SearchIndexModel(definition=VECTOR_INDEX, name=VECTOR_INDEX_NAME, type="vectorSearch"))
        print(f"  created  vector index {VECTOR_INDEX_NAME} (builds in the background)")
    except OperationFailure as e:
        print(f"  FAILED   vector index: {e.details.get('errmsg', e)}")
        print("           -> Automated Embedding may not be available; see docs/04-roadmap.md risks")


def main() -> None:
    print(f"Setting up database '{db.name}'")
    for name, validator in VALIDATORS.items():
        ensure_collection(name, validator)
    ensure_indexes()
    ensure_vector_index()
    print("Done.")


if __name__ == "__main__":
    main()
