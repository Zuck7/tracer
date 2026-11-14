"""Every MongoDB read and write in Tracer goes through this package.

The agent never imports pymongo directly. Keeping all database code here means
transactions, validators and indexes are handled in one place.
"""
