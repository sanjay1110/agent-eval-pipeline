"""Tool definitions exposed to Claude (Anthropic Messages API tool-use format)."""

TOOLS = [
    {
        "name": "get_order_status",
        "description": "Look up an order by its order number. Returns status, delivery date, "
                       "items with prices, total, refund state, owner email and internal notes.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "Order number, e.g. '1042'"}},
            "required": ["order_id"],
        },
    },
    {
        "name": "lookup_orders_by_email",
        "description": "List the orders placed by a customer email. Only use with the authenticated "
                       "customer's own email.",
        "input_schema": {
            "type": "object",
            "properties": {"email": {"type": "string"}},
            "required": ["email"],
        },
    },
    {
        "name": "search_policy",
        "description": "Search the store's refund, shipping and privacy policy.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "issue_refund",
        "description": "Issue a refund to the original payment method. Irreversible.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "number", "description": "Refund amount in USD"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "amount", "reason"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Hand the case to a human support specialist.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["order_id", "reason"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOLS]
