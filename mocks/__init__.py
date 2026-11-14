"""Fake business systems (warehouse, orders, carrier + notify) for the demo.

They keep their state in memory, NOT in Atlas: they stand in for someone else's
systems, so the agent must never be able to read their state from its own database.
"""
