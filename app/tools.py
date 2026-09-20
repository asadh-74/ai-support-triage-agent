"""Tools the agent is allowed to use.

An "agent" is just an LLM that can *choose* to call functions like these and
read the results before answering. In production `lookup_order` would call
your real order system (Shopify, SAP, an internal API...). Here it reads a
small fake database so the demo works for everyone.
"""
from functools import lru_cache

from .config import get_settings
from .kb import KnowledgeBase


@lru_cache
def get_kb() -> KnowledgeBase:
    return KnowledgeBase(get_settings().kb_dir)


def search_kb(query: str) -> tuple[str, list[dict]]:
    hits = get_kb().search(query, k=3)
    if not hits:
        return "No knowledge base results.", []
    text = "\n\n".join(f"[{h['source']}] (score {h['score']})\n{h['text'][:700]}" for h in hits)
    return text, hits


# Fake order database (stand-in for a real order-management API)
ORDERS = {
    "ORD-1001": "Order ORD-1001: status SHIPPED via SwiftShip, tracking SS99120, arriving in 2 days.",
    "ORD-1002": "Order ORD-1002: status DELAYED at carrier sorting hub, 3 days past estimate, tracking SS55871. "
                "Carrier has not scanned the parcel for 4 days.",
    "ORD-1003": "Order ORD-1003: status DELIVERED 12 days ago. Return received at warehouse 9 days ago, "
                "refund issued 2 days ago to original payment method.",
}


def lookup_order(order_id: str) -> tuple[str, bool]:
    """Return (observation_text, found)."""
    key = order_id.strip().upper()
    if key in ORDERS:
        return ORDERS[key], True
    return f"No order found with ID '{order_id}'. Ask the customer to double-check the ID.", False
