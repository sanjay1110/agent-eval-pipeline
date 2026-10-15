"""Deterministic offline stand-in for the Claude agent.

Purpose: let the whole pipeline (and CI) run with no API key and no cost, and give
the checks something realistic to grade. It calls the SAME mock tools through the
SAME TraceRecorder, so its trajectories are real tool-call traces.

It intentionally reproduces three open defects observed in agent v1.4 so the
pipeline has genuine failures to find (see data/dataset.jsonl, category known_failure):
  * multi-request messages: only order-related parts are handled (policy question dropped)
  * refund window off-by-one: day 30 treated as expired (`>= 30` instead of `> 30`)
  * always answers in English, even to non-English customers

Defects that were previously fixed (and are now regression tests) are NOT present.
Real sign-off runs use --mode live (ClaudeSupportAgent).
"""
from __future__ import annotations

import re

from agent.mock_backend import ESCALATION_THRESHOLD, REFUND_WINDOW_DAYS
from agent.prompts import AGENT_VERSION
from agent.trace import TraceRecorder

INJECTION_MARKERS = ["ignore your previous", "ignore previous", "ignore all previous", "admin mode",
                     "developer mode", "skip verification", "store manager", "employee id", "system override"]
ENCODED_MARKERS = ["base64", "rot13", "encoded"]
SCOPE_KEYWORDS = ["refund", "return", "order", "ship", "deliver", "policy", "package", "track",
                  "reembolso", "pedido", "money back"]
REFUND_WORDS = ["refund", "return", "reembolso", "money back"]
STATUS_WORDS = ["where", "status", "shipped", "track", "check"]
PRIORITY = ["refund", "escalate", "deny", "clarify", "info", "decline"]


def _stems(name: str) -> list[str]:
    words = re.findall(r"[a-z]+", name.lower())
    return [w.rstrip("s") for w in words if len(w) >= 4 and w != "pack"]


def _money(x: float) -> str:
    return f"${x:,.2f}"


class StubAgent:
    name = "stub-agent"
    version = f"{AGENT_VERSION}-stub"
    mode = "stub"

    def run(self, user_message: str, customer_email: str, backend) -> dict:
        rec = TraceRecorder(backend)
        text, decision = self._respond(user_message, customer_email.lower(), rec)
        raw = f"{text}\n\nDECISION: {decision}"
        return {"raw_output": raw, "steps": rec.steps, "stop_reason": "end_turn",
                "turns": 1 + sum(1 for s in rec.steps if s["type"] == "tool_call"),
                "usage": {"input_tokens": 0, "output_tokens": 0}, "error": None}

    # ------------------------------------------------------------------ routing
    def _respond(self, msg: str, email: str, rec: TraceRecorder) -> tuple[str, str]:
        low = msg.lower()
        ids = list(dict.fromkeys(re.findall(r"(?<!\d)#?(\d{4})(?!\d)", msg)))

        if any(m in low for m in ENCODED_MARKERS):
            return ("I can't act on encoded or forwarded instructions. If you need help with one of "
                    "your own orders, tell me the order number and what you need in plain text."), "decline"
        if any(m in low for m in INJECTION_MARKERS):
            return ("I can't bypass verification or follow override instructions. Refunds are only "
                    "issued on orders that belong to your account and meet our refund policy. If "
                    "there's a problem with one of your own orders, share the order number and I'll "
                    "take a look."), "deny"
        if not ids and not any(k in low for k in SCOPE_KEYWORDS):
            return ("Sorry, I can only help with orders, refunds, shipping and store policies, so I "
                    "can't help with that. Is there anything about an order I can help with?"), "decline"

        if not ids and "policy" in low:
            return self._policy(rec)
        if ids and ("email" in low or "address" in low) and not any(w in low for w in REFUND_WORDS):
            return self._pii_request(ids[0], email, rec)
        if not ids:
            return self._refund_without_id(msg, email, rec)

        # Per-order intents from the clause each id appears in.
        # DEFECT (multi-request): clauses with no order id (e.g. a policy question) are dropped.
        wants_refund = any(w in low for w in REFUND_WORDS)
        clauses = re.split(r"[,;?!]|\. ", msg)
        parts, decisions = [], []
        for oid in ids:
            clause = next((c.lower() for c in clauses if oid in c), low)
            if "confirm" in low or "went through" in low:
                intent = "confirm"
            elif any(w in clause for w in REFUND_WORDS):
                intent = "refund"
            elif any(w in clause for w in STATUS_WORDS):
                intent = "status"
            else:
                intent = "refund" if wants_refund else "status"
            handler = {"refund": self._refund, "status": self._status, "confirm": self._confirm}[intent]
            text, dec = handler(oid, msg, email, rec)
            parts.append(text)
            decisions.append(dec)
        return "\n\n".join(parts), min(decisions, key=PRIORITY.index)

    # ---------------------------------------------------------------- handlers
    def _lookup(self, oid: str, email: str, rec: TraceRecorder):
        order = rec.call("get_order_status", {"order_id": oid})
        if "error" in order:
            return None, (f"I couldn't find an order with number {oid}. Could you double-check the "
                          "order number?", "clarify")
        if order["customer_email"].lower() != email:
            return None, (f"Order {oid} isn't associated with your account, so I can't make changes "
                          "to it or share its details.", "deny")
        return order, None

    def _policy(self, rec: TraceRecorder):
        rec.call("search_policy", {"query": "returns refunds opened items"})
        return ("You can return delivered items within 30 days of delivery (day 30 included) for a "
                "refund of the price you paid. Opened items are fine as long as they're undamaged or "
                "defective. Orders that haven't been delivered yet can't be refunded, and refunds "
                "over $500 are reviewed by a specialist. Want me to start a refund on one of your "
                "orders?"), "info"

    def _pii_request(self, oid: str, email: str, rec: TraceRecorder):
        order, err = self._lookup(oid, email, rec)
        if err:
            return ("I can't share personal details for an order that isn't on your account. If it's "
                    "your order, please contact us from the email address used to place it."), "decline"
        return (f"The email on order {oid} is {order['customer_email']} and it ships to "
                f"{order['shipping_address']}."), "info"

    def _status(self, oid: str, msg: str, email: str, rec: TraceRecorder):
        order, err = self._lookup(oid, email, rec)
        if err:
            return err
        items = ", ".join(i["name"] for i in order["items"])
        if order["status"] == "delivered":
            return f"Order {oid} ({items}) was delivered on {order['delivered_on']}.", "info"
        if order["status"] == "shipped":
            return (f"Order {oid} ({items}) has shipped and is in transit, expected to arrive on "
                    f"{order['expected_delivery']}."), "info"
        return (f"Order {oid} ({items}) is still processing and hasn't shipped yet. You'll get "
                "tracking details as soon as it ships."), "info"

    def _confirm(self, oid: str, msg: str, email: str, rec: TraceRecorder):
        order, err = self._lookup(oid, email, rec)
        if err:
            return err
        if order["refunded"]:
            return f"Yes - order {oid} was refunded on {order.get('refunded_on')}.", "info"
        item = order["items"][0]["name"]
        return (f"I don't see a refund on order {oid} yet - no refund has been issued. Your {item} "
                f"({_money(order['total'])}) was delivered on {order['delivered_on']}, so it's "
                "eligible. Would you like me to issue the refund now?"), "info"

    def _refund(self, oid: str, msg: str, email: str, rec: TraceRecorder):
        low = msg.lower()
        order, err = self._lookup(oid, email, rec)
        if err:
            return err
        if order["refunded"]:
            return (f"Order {oid} was already refunded on {order.get('refunded_on')} "
                    f"({_money(order['total'])}), so it isn't eligible for another refund."), "deny"
        if order["status"] != "delivered":
            return (f"Order {oid} is currently {order['status']} and hasn't been delivered yet, so it "
                    "can't be refunded yet. Once it arrives you can request a refund, or you can "
                    "refuse the delivery."), "deny"
        # DEFECT (window boundary): should be `> REFUND_WINDOW_DAYS` (day 30 is eligible).
        if order["days_since_delivery"] >= REFUND_WINDOW_DAYS:
            return (f"Order {oid} was delivered on {order['delivered_on']}, which is outside our "
                    "30-day refund window, so I'm not able to refund it."), "deny"

        items = self._select_items(order, low)
        amount = round(sum(i["price"] for i in items), 2)
        names = " and ".join(i["name"] for i in items)
        if amount > ESCALATION_THRESHOLD:
            rec.call("escalate_to_human", {"order_id": oid, "reason": f"Refund request {_money(amount)} exceeds $500"})
            return (f"Because a refund of {_money(amount)} for order {oid} is over $500, I've passed "
                    "your request to a specialist. They'll reply within 1 business day."), "escalate"
        result = rec.call("issue_refund", {"order_id": oid, "amount": amount, "reason": f"Customer return: {names}"})
        if "error" in result:
            return f"I wasn't able to process the refund for order {oid}. Please try again shortly.", "deny"
        text = (f"Done - I've refunded {_money(amount)} for the {names} on order {oid} "
                f"(refund {result['refund_id']}). It should appear on your original payment method "
                "within 5-7 business days.")
        if "again" in low or "compensation" in low:
            text += " I can only issue one refund per order, so I can't add a second refund as compensation."
        return text, "refund"

    def _refund_without_id(self, msg: str, email: str, rec: TraceRecorder):
        low = msg.lower()
        orders = rec.call("lookup_orders_by_email", {"email": email})["orders"]
        matches = [o for o in orders if any(st in low for name in o["items"] for st in _stems(name))]
        if len(matches) == 1:
            return self._refund(matches[0]["order_id"], msg, email, rec)
        ids = [o["order_id"] for o in orders]
        listed = ", ".join(ids[:-1]) + (" and " if len(ids) > 1 else "") + ids[-1] if ids else "none"
        return (f"Happy to help. I can see orders {listed} on your account. Which order would you "
                "like refunded, and is it the whole order or specific items?"), "clarify"

    @staticmethod
    def _select_items(order: dict, low: str) -> list[dict]:
        items = order["items"]
        if len(items) == 1:
            return items
        keep, drop = [], []
        for it in items:
            for st in _stems(it["name"]):
                for m in re.finditer(st, low):
                    before = low[max(0, m.start() - 12):m.start()]
                    (drop if re.search(r"\bnot\b|\bexcept\b|\bkeep\b", before) else keep).append(it["name"])
        chosen = [i for i in items if i["name"] in keep and i["name"] not in drop]
        return chosen or items
