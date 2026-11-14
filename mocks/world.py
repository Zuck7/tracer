"""The large synthetic world around the two recalled lots. All data is synthetic.

One deterministic generator (fixed random seed) feeds every system, so the
separate mock processes agree with each other: an order's tracking number is a
real carrier shipment, and every lot in the warehouse is a lot in Northfield's
production records (store/catalogue.py).

Scale: ~650 lots across 10 products, ~5,000 cases in the warehouse, 15,000
orders from 6,000 customers, ~13,000 shipments. The recalled lots (GB-2611,
OB-3107) keep their small, fixed seed in mocks/seed.py, so a recall is a needle
in a realistic haystack: Tracer must find exactly those items and touch nothing else.

Lot codes are chosen so the warehouse's fuzzy search (first 5 characters) only
returns the one deliberate decoy next to each recalled lot (GB-2612, OB-3106).
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

SEED = 2611
N_CUSTOMERS = 6000
N_ORDERS = 15000

# prefix, product, sku, allergens on the label, lot number ranges (avoid GB-26xx / OB-31xx)
PRODUCTS = [
    ("GB", "Oat & honey granola bars", "GRB-OAT-12", ["oats", "honey"], [(2401, 2460), (2501, 2560)]),
    ("OB", "Apple oat bars", "OBR-APL-08", ["oats"], [(3001, 3060), (3201, 3240)]),
    ("TM", "Trail mix with peanuts", "TMX-PNT-10", ["peanuts", "tree nuts"], [(1101, 1149), (1151, 1170)]),
    ("PB", "Chocolate protein bars", "PRB-CHC-10", ["milk", "soy"], [(4001, 4070)]),
    ("MU", "Fruit muesli", "MSL-FRT-05", ["oats", "wheat"], [(5001, 5060)]),
    ("SB", "Pumpkin seed bars", "SDB-PMP-08", ["sesame"], [(5501, 5560)]),
    ("OC", "Oat cookies", "OCK-OAT-12", ["oats", "wheat", "milk"], [(6001, 6070)]),
    ("CR", "Sea salt rice crackers", "CRK-SEA-07", [], [(6501, 6560)]),
    ("NB", "Peanut butter bites", "NBB-PNT-06", ["peanuts"], [(7001, 7060)]),
    ("FB", "Berry fruit bars", "FRB-BRY-10", [], [(7501, 7560)]),
]

ROUTINE = [
    "Tested clean.", "Routine micro test passed.", "Metal detection check passed.",
    "Released after a negative allergen swab of the line.",
    "Label reprinted for a date-format typo; no safety impact.",
    "Moisture at the top of spec; released.", "No issues.",
    "Customer complaint about texture; no safety issue found.",
    "Peanut swab on the line was negative before production.",
    "Tree nut swab on the line was negative before production.",
]


@lru_cache(maxsize=1)
def lots() -> list[dict[str, Any]]:
    """Every background lot, in Northfield's production-record shape."""
    rng = random.Random(SEED)
    out = []
    for prefix, product, sku, allergens, ranges in PRODUCTS:
        numbers = [n for lo, hi in ranges for n in range(lo, hi + 1)]
        for i, n in enumerate(numbers):
            produced = date(2025, 1, 6) + timedelta(days=int(i * 670 / max(1, len(numbers) - 1)))
            finding = rng.choice(ROUTINE)
            if allergens and rng.random() < 0.3:
                finding = f"Contains {', '.join(allergens)}, declared on the label. {finding}"
            out.append({"lot": f"{prefix}-{n}", "supplier": "Northfield Foods", "product": product,
                        "sku": sku, "produced": produced.isoformat(),
                        "line": f"Line {rng.randint(1, 4)}", "allergens_on_label": allergens,
                        "qa_finding": finding, "recall_id": None})
    return out


@lru_cache(maxsize=1)
def _build() -> dict[str, Any]:
    rng = random.Random(SEED + 1)
    all_lots = lots()
    recent = [lot for lot in all_lots if lot["produced"] >= "2026-03-01"]

    units: dict[str, dict] = {}
    n = 100000
    for lot in all_lots:
        on_hand = rng.randint(4, 30) if lot in recent else rng.choice([0, 0, 0, 1, 2, 3])
        for _ in range(on_hand):
            n += 1
            units[f"UNIT-{n}"] = {"unit_id": f"UNIT-{n}", "sku": lot["sku"], "lot": lot["lot"],
                                  "location": f"{rng.choice('ABCDEFGH')}-{rng.randint(1, 40):02d}-"
                                              f"{rng.randint(1, 6):02d}",
                                  "status": "available", "hold": None}

    orders: dict[str, dict] = {}
    shipments: dict[str, dict] = {}
    for i in range(N_ORDERS):
        oid, trk = f"ORD-{100001 + i}", f"TRK-{100001 + i}"
        lot = rng.choice(recent if rng.random() < 0.8 else all_lots)
        status = rng.choices(["delivered", "shipped", "open", "cancelled"], [70, 10, 8, 12])[0]
        orders[oid] = {"order_id": oid, "customer_id": f"CUST-{20001 + rng.randrange(N_CUSTOMERS)}",
                       "lot": lot["lot"], "status": status,
                       "tracking_no": None if status == "cancelled" else trk, "cancel": None}
        if status != "cancelled":
            shipments[trk] = {"tracking_no": trk, "order_id": oid, "lot": lot["lot"],
                              "status": {"delivered": "delivered", "shipped": "in_transit",
                                         "open": "label_created"}[status],
                              "intercept": None}
    return {"units": units, "orders": orders, "shipments": shipments}


def units() -> dict[str, dict]:
    return {k: dict(v) for k, v in _build()["units"].items()}


def orders() -> dict[str, dict]:
    return {k: dict(v) for k, v in _build()["orders"].items()}


def shipments(pickup_at: str) -> dict[str, dict]:
    return {k: {**v, "pickup_at": pickup_at} for k, v in _build()["shipments"].items()}
