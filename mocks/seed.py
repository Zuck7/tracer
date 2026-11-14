"""Synthetic seed data. Matches docs/03-contracts.md section 7.

In scope (IDs and counts are fixed; the docs, tests and demo script depend on them):
  Recall 1  RC-GB-2611  lot GB-2611  20 holds + 10 cancels + 8 intercepts + 22 notices = 60 steps
  Recall 2  RC-OB-3107  lot OB-3107   8 holds +  4 cancels + 4 intercepts +  8 notices = 24 steps
  Decoy     UNIT-0099   lot GB-2612  returned by the fuzzy warehouse search; the policy gate must deny it

Background (everything else a real business would have): 15 other lots of 8 products across
3 warehouse sites, about 240 more units, 180 more orders and their shipments. None of it is in
either recall, so the agent has to find the recalled lot among normal traffic and leave the
rest alone. It is generated from fixed seeds, so every reset gives exactly the same data.
"""
import random
from datetime import datetime, timedelta, timezone

PICKUP_IN_MINUTES = 10  # countdown to the next truck pickup, from reset time
TODAY = datetime(2026, 11, 14, tzinfo=timezone.utc)

SITES = ["TOR-1", "MIS-2", "VAU-3"]
PRODUCTS = {  # lot prefix -> (product name, SKUs by pack size)
    "GB": ("Granola bars, oats & honey", ["GRB-OAT-12", "GRB-OAT-24"]),
    "OB": ("Oat bars, apple cinnamon", ["OAT-BAR-06", "OAT-BAR-18"]),
    "PB": ("Protein bars, cocoa", ["PRO-COC-12"]),
    "TM": ("Trail mix, classic", ["TRM-CLS-08", "TRM-CLS-20"]),
    "RK": ("Rice cakes, sea salt", ["RCK-SLT-10"]),
    "FS": ("Fruit snacks, mixed berry", ["FRS-BER-24"]),
    "NB": ("Nut butter cups, almond", ["NBC-ALM-12"]),
    "CK": ("Crackers, whole wheat", ["CRK-WHT-06", "CRK-WHT-12"]),
}
BACKGROUND_LOTS = ["GB-2540", "GB-2702", "OB-3055", "OB-3210", "PB-4102", "PB-4188", "TM-5120", "TM-5177",
                   "RK-6031", "RK-6090", "FS-7215", "FS-7260", "NB-8104", "CK-9042", "CK-9110"]
# The warehouse search is fuzzy on the first 5 characters of the lot. Background lots must not
# share a prefix with a recalled lot, or they would show up as extra decoys.
assert not any(lot[:5] in ("GB-26", "OB-31") for lot in BACKGROUND_LOTS)

CITIES = [("Toronto", "ON"), ("Mississauga", "ON"), ("Ottawa", "ON"), ("Hamilton", "ON"), ("London", "ON"),
          ("Montreal", "QC"), ("Quebec City", "QC"), ("Vancouver", "BC"), ("Calgary", "AB"),
          ("Edmonton", "AB"), ("Winnipeg", "MB"), ("Halifax", "NS")]
CHANNELS = ["web", "web", "web", "marketplace", "wholesale"]


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _num(some_id: str) -> int:
    return int(some_id.split("-")[1])


def _sku(lot: str, n: int) -> str:
    skus = PRODUCTS[lot[:2]][1]
    return skus[n % len(skus)]


def _expires(lot: str) -> str:
    """Best-before date, derived from the lot number so it is stable."""
    return (TODAY + timedelta(days=90 + _num(lot) % 240)).date().isoformat()


def _ship_to(order_id: str) -> dict:
    city, province = CITIES[_num(order_id) % len(CITIES)]
    return {"city": city, "province": province}


def _order_details(order_id: str, lot: str) -> dict:
    """Realistic extra fields, computed from the order number so orders and carrier agree."""
    n = _num(order_id)
    return {"sku": _sku(lot, n), "qty": 1 + (n * 7) % 4, "channel": CHANNELS[n % len(CHANNELS)],
            "ship_to": _ship_to(order_id), "placed_at": _iso(TODAY - timedelta(days=n % 19, hours=n % 11))}


def _background_orders() -> list[tuple]:
    """(order_id, customer_id, lot, status, tracking_no) for orders outside both recalls.

    Same list on every call (fixed seed), because the orders mock and the carrier mock
    each build their own copy and the two must agree.
    """
    rng = random.Random(3107)
    rows = []
    for n in range(180):
        # 1 in 8 is a repeat order from a customer who also bought a recalled lot.
        customer = (f"CUST-{rng.choice(list(range(101, 123)) + list(range(201, 209)))}"
                    if rng.random() < 0.125 else f"CUST-{rng.randint(301, 440)}")
        status = rng.choices(["delivered", "shipped", "open", "cancelled"], weights=[55, 20, 15, 10])[0]
        tracking = None if status == "cancelled" else f"TRK-{8001 + n}"
        rows.append((f"ORD-{3001 + n}", customer, rng.choice(BACKGROUND_LOTS), status, tracking))
    return rows


def inventory() -> dict[str, dict]:
    units = {}

    def add(unit_id, sku, lot, site, location):
        units[unit_id] = {"unit_id": unit_id, "sku": sku, "product": PRODUCTS[lot[:2]][0], "lot": lot,
                          "site": site, "location": location, "expires": _expires(lot),
                          "status": "available", "hold": None}

    # Recall 1: one lot, two pack sizes, spread over two sites.
    for i in range(1, 21):
        add(f"UNIT-{i:04d}", "GRB-OAT-12" if i <= 12 else "GRB-OAT-24", "GB-2611",
            "TOR-1" if i <= 14 else "MIS-2", f"A-{(i - 1) // 5 + 1:02d}-{(i - 1) % 5 + 1:02d}")
    add("UNIT-0099", "GRB-OAT-12", "GB-2612", "TOR-1", "A-05-01")          # decoy, different lot
    # Recall 2
    for i in range(101, 109):
        add(f"UNIT-{i:04d}", "OAT-BAR-06", "OB-3107", "VAU-3", f"B-01-{i - 100:02d}")
    # Background stock
    rng = random.Random(2611)
    n = 1001
    for lot in BACKGROUND_LOTS:
        for _ in range(rng.randint(12, 20)):
            add(f"UNIT-{n}", _sku(lot, n), lot, rng.choice(SITES),
                f"{rng.choice('CDEFGH')}-{rng.randint(1, 12):02d}-{rng.randint(1, 6):02d}")
            n += 1
    return units


def orders() -> dict[str, dict]:
    out = {}

    def add(order_id, customer_id, lot, status, tracking_no=None):
        out[order_id] = {"order_id": order_id, "customer_id": customer_id, "lot": lot,
                         "status": status, "tracking_no": tracking_no, "cancel": None,
                         **_order_details(order_id, lot)}

    # Recall 1
    for n in range(10):                                   # open (label printed, awaiting pickup)
        add(f"ORD-{1001 + n}", f"CUST-{101 + n}", "GB-2611", "open", f"TRK-{7001 + n}")
    for n in range(8):                                    # on trucks
        add(f"ORD-{1011 + n}", f"CUST-{111 + n}", "GB-2611", "shipped", f"TRK-{5001 + n}")
    for n in range(4):                                    # already in homes
        add(f"ORD-{1019 + n}", f"CUST-{119 + n}", "GB-2611", "delivered", f"TRK-{4990 + n}")
    # Recall 2
    for n in range(4):
        add(f"ORD-{2001 + n}", f"CUST-{201 + n}", "OB-3107", "open", f"TRK-{7101 + n}")
    for n in range(4):
        add(f"ORD-{2005 + n}", f"CUST-{205 + n}", "OB-3107", "shipped", f"TRK-{6001 + n}")
    # Background orders
    for row in _background_orders():
        add(*row)
    return out


def shipments() -> dict[str, dict]:
    pickup = _iso(datetime.now(timezone.utc) + timedelta(minutes=PICKUP_IN_MINUTES))
    out = {}

    def add(tracking_no, order_id, lot, status):
        out[tracking_no] = {"tracking_no": tracking_no, "order_id": order_id, "lot": lot,
                            "status": status, "pickup_at": pickup, "intercept": None,
                            "service": "express" if _num(tracking_no) % 4 == 0 else "ground",
                            "destination": _ship_to(order_id)}

    for n in range(8):
        add(f"TRK-{5001 + n}", f"ORD-{1011 + n}", "GB-2611", "in_transit")
    for n in range(4):
        add(f"TRK-{4990 + n}", f"ORD-{1019 + n}", "GB-2611", "delivered")
    for n in range(4):
        add(f"TRK-{6001 + n}", f"ORD-{2005 + n}", "OB-3107", "in_transit")
    # Labels printed for open orders: the truck collects them at the pickup time.
    # Not on a truck yet, so the agent cancels the order instead of intercepting.
    for n in range(10):
        add(f"TRK-{7001 + n}", f"ORD-{1001 + n}", "GB-2611", "label_created")
    for n in range(4):
        add(f"TRK-{7101 + n}", f"ORD-{2001 + n}", "OB-3107", "label_created")
    # Background shipments, one per background order that has a label.
    carrier_status = {"open": "label_created", "shipped": "in_transit", "delivered": "delivered"}
    for order_id, _customer, lot, status, tracking_no in _background_orders():
        if tracking_no:
            add(tracking_no, order_id, lot, carrier_status[status])
    return out
