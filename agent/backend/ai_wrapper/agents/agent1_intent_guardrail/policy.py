"""Local, instant crisis check that runs before any model call.

Two layers:

1. ``_FIRST_PERSON_RISK``: the person talks about *their own* death, a plan, or harming
   someone. These always count, whatever else the message says.
2. ``_BARE_RISK_WORDS``: a mention of "suicide", "self-harm", "overdose" and so on. On its
   own this is only a crisis when the message is not a bereavement story. "My father died
   by suicide" is grief, not a crisis; "my father died by suicide and I'm thinking of doing
   the same" is both, and still counts.

The check is deliberately conservative: when a message is ambiguous it is treated as a
crisis. Anything it lets through still reaches Agent 1, which makes the nuanced call.
"""

import re

_FIRST_PERSON_RISK = (
    r"\b(?:i\s+)?(?:want|wanna|wish|plan(?:ning)?|trying|ready|decided)\s+(?:to\s+)?(?:die|be dead|suic[a-z]*|sucide|kill myself|end it all|end my life)\b",
    r"\b(?:is there any|how to|what is the|ways? to|methods? to)\s+(?:way to\s+)?(?:die|suic[a-z]*|sucide|kill myself|end my life)\b",
    r"\b(?:how|ways?|methods?|where)\s+(?:can\s+i|to|should\s+i)\s+(?:die|kill\s+myself|end\s+my\s+life)\b",
    r"\b(?:kill|hurt|end|harm)\s+(?:myself|my life|my own life)\b",
    r"\b(?:end|take)\s+my\s+(?:own\s+)?life\b",
    r"\b(?:bought|have|got|holding|using)\s+(?:the\s+)?(?:pills|gun|weapon|razor|rope|knife|bleach|poison).{0,45}\b(?:tonight|today|now|use|take|drink|swallow)\b",
    r"\b(?:taking|take|swallow(?:ing)?)\s+(?:the|these|all my|my)?\s*pills\s+(?:tonight|today|now)\b",
    r"\bi(?:'m| am|'ll| will)\s+(?:going\s+to|gonna)\s+(?:die|be dead|kill myself|hurt myself|end it all|end my (?:own )?life|take my (?:own )?life|suic[a-z]*)\b",
    r"\b(?:i\s+)?(?:am|feel|feeling)\s+(?:suicidal|wanting to die)\b",
    # Harming someone else: needs first-person intent. "My dad hurt her" is a story, not a threat.
    r"\b(?:i(?:'m| am)\s+(?:going to|gonna|about to|planning to|ready to)|i(?:'ll| will| would|'d| might| may)|i\s+(?:want|wanna|need|plan|decided|intend)\s+to|gonna|going to|want to|wanna|about to)\s+(?:really\s+|just\s+)?(?:kill|hurt|harm)\s+(?:someone|somebody|everyone|them|him|her|my\s+\w+)\b",
)

_BARE_RISK_WORDS = r"\b(?:suicide|sucide|suiside|suicde|suicidal|self[- ]harm|overdose)\b"

# Someone else's death, or a past loss. Used only to *exempt* a bare risk word; a message that
# is not recognised as a loss story stays a crisis (the safe default).
_LOSS_NARRATIVE = (
    r"\b(?:died|dying)\s+(?:by|from|of|due to|because of)\s+(?:a\s+|an\s+)?(?:accidental\s+)?(?:suicide|sucide|suiside|suicde|overdose)\b",
    r"\b(?:killed|took|ended)\s+(?:himself|herself|themselves|his own life|her own life|their own life|his life|her life|their life)\b",
    r"\b(?:committed|completed)\s+suicide\b",
    r"\blost\s+(?:my|our|a|an)\s+[a-z' -]{0,30}?\s*to\s+(?:suicide|sucide|an overdose|overdose)\b",
    r"\b(?:death|died)\s+(?:by|from|of|to)\s+(?:suicide|sucide)\b",
    r"\b(?:suicide|sucide)\s+(?:loss|bereavement|survivors?|grief|death)\b",
    r"\bsurvivors?\s+of\s+(?:a\s+)?(?:suicide|sucide)\b",
    r"\b(?:his|her|their)\s+(?:own\s+)?(?:suicide|sucide)\b",
    r"\b[a-z]+'s\s+(?:suicide|sucide)\b",
    r"\b(?:suicide|sucide)\s+of\s+(?:my|our|his|her|their|a|the)\b",
)

# Inside a loss story: the writer's own risk. These need first-person framing, because the
# story itself will contain words like "suicidal" about the person who died.
_RISK_WITHIN_LOSS = (
    r"\b(?:i|i'm|i am|i've|i have|i keep|i can't stop)\b[^.!?]{0,30}\b(?:thinking|thought|thoughts|think)\s+(?:of|about)\s+(?:suicide|sucide|killing myself|ending (?:it|my life)|doing the same|following|joining)",
    r"\b(?:want|wanna|going|gonna|plan(?:ning)?|ready|decided|thinking|think|might|may|could|tempted|considering|trying|need)\b[^.!?]{0,20}\b(?:do|doing)\s+(?:the\s+same|what\s+(?:he|she|they)\s+did)\b",
    r"\b(?:want|wanna|going|gonna|plan(?:ning)?|ready|decided|thinking|think|might|may|could|tempted|considering|trying|need)\b[^.!?]{0,20}\b(?:follow|join)\s+(?:him|her|them|my\s+\w+)\b(?!\W+(?:\w+\W+){0,2}footsteps)",
    r"\b(?:everyone|everybody|they|my family|the world)\s+(?:would|will)\s+be\s+better\s+off\s+without\s+me\b",
    r"\b(?:don'?t|do not)\s+want\s+to\s+(?:live|be here|be alive|go on|exist)\b",
    r"\bno\s+(?:reason|point)\s+(?:to|in)\s+(?:live|living|going on|continue|continuing)\b",
    r"\bwish\s+(?:i|it)\s+(?:was|were)\s+(?:me|dead|gone)\b",
)


def _any(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def has_immediate_crisis_language(text: str) -> bool:
    normalized = " ".join(text.lower().replace("\u2019", "'").split())

    if _any(_FIRST_PERSON_RISK, normalized):
        return True
    is_loss_story = _any(_LOSS_NARRATIVE, normalized)
    if is_loss_story and _any(_RISK_WITHIN_LOSS, normalized):
        return True
    if re.search(_BARE_RISK_WORDS, normalized):
        return not is_loss_story
    return False
