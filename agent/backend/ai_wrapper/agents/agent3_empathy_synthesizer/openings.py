"""Detects replies that open by echoing the user's words or feelings back at them.

The prompt already forbids this, but models drift back to it ("It sounds like you're feeling...").
The orchestrator uses this check to ask for one rewrite instead of trusting the prompt alone.
The patterns are narrow on purpose: personal warmth ("I'm so sorry about your dad", "I understand")
is what we want; only formulaic mirroring and impersonal, article-style openers are flagged.
"""

import re

OPENING_FEEDBACK = (
    "Rewrite the first sentence. Open with a warm, personal line addressed to the person, using "
    "\"I\" and \"you\" (for example \"I'm so sorry about ...\", \"I understand, and I'm really glad "
    "you told me\", or \"I'm here with you\"). Do not start with \"It sounds like\", \"That sounds\", "
    "\"I hear that\" or \"You're feeling\", do not repeat back what they said, and do not open with a "
    "general statement about grief or people. Then go straight into a helpful, caring reply. "
    "Keep the rest."
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


def _first_sentence(text: str) -> str:
    stripped = text.strip().lstrip("#>*_-\"'\u201c\u2018 \t")
    stripped = stripped.replace("\u2019", "'")
    return re.split(r"(?<=[.!?\u2026])\s|\n", stripped, maxsplit=1)[0][:200].lower()


def has_mirroring_opening(text: str) -> bool:
    sentence = _first_sentence(text)
    return any(re.search(pattern, sentence) for pattern in _MIRRORING_OPENINGS)
