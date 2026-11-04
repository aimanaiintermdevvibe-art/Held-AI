"""Detects replies that open in a formulaic way instead of engaging with the user's message.

The prompt already forbids these, but models drift back to them. The orchestrator uses these
checks to ask for one rewrite instead of trusting the prompt alone. Two kinds are flagged:

- mirroring: echoing the user's words or feelings back ("It sounds like you're feeling..."),
  or an impersonal, article-style opener about people or grief;
- stock phrases: set sympathy, gratitude or reassurance lines ("I'm so sorry to hear that",
  "I'm really glad you told me", "Thank you for sharing", "I understand"). Crisis replies are
  exempt: there, a line like "I'm really glad you told me" is good practice.

The patterns look only at the first sentence and are narrow on purpose, so a reply that opens
with something specific to the person passes.
"""

import re

OPENING_FEEDBACK = (
    "Rewrite the first sentence. Open by engaging with the specific thing they said or asked, in "
    "your own words. Do not start with \"It sounds like\", \"That sounds\", \"I hear that\" or "
    "\"You're feeling\", do not repeat back what they said, and do not open with a general "
    "statement about grief or people. Keep the rest."
)
STOCK_OPENING_FEEDBACK = (
    "Rewrite the first sentence without a stock phrase such as \"I'm so sorry\", \"I'm glad\", "
    "\"Thank you for sharing\", \"I understand\" or \"I'm here\". Open by engaging with the specific "
    "thing they said or asked, in your own words. Keep the rest, and do not move the stock phrase "
    "somewhere else in the reply."
)

_MIRRORING_OPENINGS = (
    r"^(?:it|that|this)\s+(?:sounds|seems|feels|appears)\b",
    r"^sounds\s+like\b",
    r"^i\s+(?:can\s+)?(?:hear|sense|notice)\s+that\b",
    r"^i(?:'m| am)\s+(?:hearing|sensing|noticing)\b",
    r"^what\s+(?:i(?:'m| am)\s+hearing|i\s+hear|you(?:'re| are)\s+(?:describing|going through|feeling|carrying|experiencing))\b",
    r"^you(?:'re| are|'ve| have)\s+(?:been\s+|really\s+|clearly\s+|so\s+)?(?:feeling|carrying|going through|dealing with|struggling|experiencing|navigating|holding|facing|telling me|saying|describing)\b",
    # Impersonal openers: a general remark about people or grief instead of speaking to the person.
    r"^[a-z]+ing\b[^,.;:]{0,80}\b(?:can|could|must|may)\s+(?:feel|be|seem|hurt|weigh)\b",
    r"^(?:grief|love|anxiety|many people|most people|people|everyone|there is no|there's no|when (?:a|someone|people|we))\b",
)

_ADVERBS = r"(?:(?:so|really|truly|very|deeply|incredibly|genuinely|just|completely|totally)\s+)*"
_STOCK_OPENINGS = (
    rf"^(?:oh,?\s+)?i(?:'m| am)\s+{_ADVERBS}(?:sorry|glad|happy|grateful|thankful|relieved)\b",
    rf"^(?:oh,?\s+)?i\s+{_ADVERBS}(?:understand|appreciate|hear you|hear how)\b",
    r"^(?:oh,?\s+)?thank(?:s| you)\b",
    rf"^(?:oh,?\s+)?i(?:'m| am)\s+{_ADVERBS}(?:here|with you)\b",
    r"^(?:oh,?\s+)?i\s+can\s+(?:only\s+)?imagine\b",
    r"^(?:oh,?\s+)?you(?:'re| are)\s+not\s+alone\b",
    rf"^(?:oh,?\s+)?(?:that|this)(?:'s| is)\s+{_ADVERBS}(?:hard|tough|difficult|a lot|heartbreaking|awful|wonderful|great|amazing)\b",
    r"^(?:oh,?\s+)?what\s+a\s+(?:lot|hard|tough|difficult|heavy|wonderful|lovely)\b",
)


def _first_sentence(text: str) -> str:
    stripped = text.strip().lstrip("#>*_-\"'“‘ \t")
    stripped = stripped.replace("’", "'")
    return re.split(r"(?<=[.!?…])\s|\n", stripped, maxsplit=1)[0][:200].lower()


def has_mirroring_opening(text: str) -> bool:
    sentence = _first_sentence(text)
    return any(re.search(pattern, sentence) for pattern in _MIRRORING_OPENINGS)


def has_stock_opening(text: str) -> bool:
    sentence = _first_sentence(text)
    return any(re.search(pattern, sentence) for pattern in _STOCK_OPENINGS)
