"""Synthetic seed data for both recalls. Matches docs/03-contracts.md section 7.

Recall 1  RC-GB-2611  lot GB-2611  20 holds + 10 cancels + 8 intercepts + 22 notices = 60 steps
Recall 2  RC-OB-3107  lot OB-3107   8 holds +  4 cancels + 4 intercepts +  8 notices = 24 steps
Decoy     UNIT-0099   lot GB-2612  returned by the fuzzy warehouse search; the policy gate must deny it
"""
from datetime import datetime, timedelta, timezone

PICKUP_IN_MINUTES = 10  # countdown to the next truck pickup, from reset time


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def inventory() -> dict[str, dict]:
    units = {}

    def add(unit_id, sku, lot, location):
        units[unit_id] = {"unit_id": unit_id, "sku": sku, "lot": lot, "location": location,
                          "status": "available", "hold": None}

    for i in range(1, 21):
        add(f"UNIT-{i:04d}", "GRB-OAT-12", "GB-2611", f"A-{(i - 1) // 5 + 1:02d}-{(i - 1) % 5 + 1:02d}")
    add("UNIT-0099", "GRB-OAT-12", "GB-2612", "A-05-01")          # decoy, different lot
    for i in range(101, 109):
        add(f"UNIT-{i:04d}", "OAT-BAR-06", "OB-3107", f"B-01-{i - 100:02d}")
    return units


def orders() -> dict[str, dict]:
    out = {}

    def add(order_id, customer_id, lot, status, tracking_no=None):
        out[order_id] = {"order_id": order_id, "customer_id": customer_id, "lot": lot,
                         "status": status, "tracking_no": tracking_no, "cancel": None}

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
    return out


def shipments() -> dict[str, dict]:
    pickup = _iso(datetime.now(timezone.utc) + timedelta(minutes=PICKUP_IN_MINUTES))
    out = {}

    def add(tracking_no, order_id, lot, status):
        out[tracking_no] = {"tracking_no": tracking_no, "order_id": order_id, "lot": lot,
                            "status": status, "pickup_at": pickup, "intercept": None}

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
    return out
