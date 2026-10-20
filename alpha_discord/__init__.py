"""Read-only ingestion of explicitly authorized Discord signal messages.

Parsing never submits orders or authenticates a message's source. Source
verification belongs to the collector; broker and policy verification belong
to a separately authorized executor.
"""

from .parser import parse_message

__all__ = ["parse_message"]
