"""Searching a user's earlier chats when a message refers back to one (PostgreSQL full-text
search; no embeddings). Shared by the database modules."""
import re
from typing import Any, Dict, List, Optional

# Words that say nothing about what to look up ("do you remember what we talked about").
_STOP = frozenset("""
a about above after again ago all also am an and any are as at be been before being but by can could
day days did do does doing down during each earlier few for from had has have having he her here hers
him his how i if in into is it its just last like me month months more most my no nor not now of off on
once only or other our out over own previous remember same said say she should so some such talk talked
talking tell than that the their them then there these they this those through time to told too under
until up very was we week weeks were what when where which while who whom why will with would yesterday
you your yours chat chats conversation conversations discussed discuss mentioned
""".split())


def search_terms(text: str, limit: int = 8) -> str:
    """An OR query for to_tsquery('english', ...) from the distinctive words of text; "" when
    there are none (then the latest chats are recalled instead)."""
    words: List[str] = []
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(word) > 2 and word not in _STOP and word not in words:
            words.append(word)
    return " | ".join(words[:limit])


def _who(role: Optional[str]) -> str:
    return "you" if role == "assistant" else "user"


def recalled_message(title: Optional[str], date: Optional[str], role: str, text: str, reply: Optional[str]) -> Dict[str, Any]:
    """One matching message from an earlier chat; for a user's message, the reply that followed."""
    item: Dict[str, Any] = {"chat": title, "date": (date or "")[:10] or None, "said_by": _who(role), "text": text}
    if reply:
        item["your_reply"] = reply
    return item


def recalled_chat(title: Optional[str], date: Optional[str], summary: Optional[str], recent: Any) -> Dict[str, Any]:
    """One of the latest earlier chats: its summary (when it has one) and last few messages."""
    item: Dict[str, Any] = {"chat": title, "date": (date or "")[:10] or None}
    if summary:
        item["summary"] = summary
    item["last_messages"] = [{"said_by": _who(m.get("role")), "text": m.get("content")} for m in recent or []]
    return item
