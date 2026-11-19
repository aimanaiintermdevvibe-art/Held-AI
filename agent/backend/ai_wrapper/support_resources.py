"""Verified support lines, by country, for crisis and other replies.

The agents may give only these numbers and links; none are written from the model's memory.
Checked on 2026-11-19 against the services' own sites and public directories (988lifeline.org,
poison.org, thehotline.org, crisistextline.org, anad.org, afsp.org, allianceofhope.org). Review
them before launch and whenever a service changes.

The country comes from the SUPPORT_COUNTRY setting (US by default), because the Held database
does not record a user's country. A country with no list here gets Find A Helpline.
"""
from typing import Any

FIND_A_HELPLINE = "https://findahelpline.com"

_US: dict[str, Any] = {
    "country": "US",
    "emergency": "911, for immediate danger: police, ambulance or fire",
    "lines": [
        {"for": "suicidal thoughts or any emotional crisis",
         "name": "988 Suicide & Crisis Lifeline",
         "contact": "call or text 988, or chat at 988lifeline.org (24/7; press 1 for veterans, 2 for Spanish)"},
        {"for": "a possible overdose or poisoning",
         "name": "Poison Control",
         "contact": "call 1-800-222-1222 (24/7) or use poison.org; call 911 if they collapse, have a seizure or trouble breathing"},
        {"for": "abuse or domestic violence",
         "name": "National Domestic Violence Hotline",
         "contact": "call 1-800-799-7233, text START to 88788, or chat at thehotline.org (24/7)"},
        {"for": "anyone who would rather text",
         "name": "Crisis Text Line",
         "contact": "text HOME to 741741 (24/7)"},
        {"for": "eating disorders (a support line, not a crisis line)",
         "name": "ANAD Helpline",
         "contact": "call 1-888-375-7767 (weekdays)"},
        {"for": "people who lost someone to suicide",
         "name": "AFSP and Alliance of Hope",
         "contact": "afsp.org/loss (support groups, peer Healing Conversations) and allianceofhope.org (24/7 online forum)"},
    ],
}

_ANYWHERE: dict[str, Any] = {
    "country": None,
    "emergency": "the local emergency number",
    "lines": [
        {"for": "any crisis, in their country", "name": "Find A Helpline", "contact": FIND_A_HELPLINE},
    ],
}

# The line a crisis reply must carry (added by code when the reply has given no line), and how to
# recognise that one was given: any number from the country's list counts, so a reply that already
# sent them to 911 and Poison Control is not given 911 a second time.
_CRISIS_LINE = {
    "US": (("988", "911", "741741", "1-800-222-1222", "1-800-799-7233"),
           "You can call or text **988** any time, day or night, to talk with someone right now. "
           "If you're in immediate danger, call **911**."),
    None: (("findahelpline",),
           f"You can find a free, confidential helpline in your country at {FIND_A_HELPLINE}. "
           "If you're in immediate danger, please call your local emergency number."),
}

_CRISIS_FALLBACK = {
    "US": ("What you just shared matters, and I'm staying right here with you. If you're in immediate "
           "danger, please call **911** now. You can also call or text **988** any time, day or night, "
           "to talk with someone right away.\n\n**Are you safe right now?**"),
    None: ("I'm really glad you reached out, and I'm staying right here with you. If you are in "
           f"immediate danger, please contact local emergency services or visit {FIND_A_HELPLINE} right now."),
}


def _key(country: str | None) -> str | None:
    code = (country or "").strip().upper()
    return code if code == "US" else None


def support_resources(country: str | None) -> dict[str, Any]:
    """The verified lines for the country, given to the writer and the auditor as data."""
    return _US if _key(country) == "US" else _ANYWHERE


def crisis_line(country: str | None) -> tuple[tuple[str, ...], str]:
    """(markers, sentence): the crisis line a crisis reply must carry, and how to spot that a
    reply already gave one."""
    return _CRISIS_LINE[_key(country)]


def crisis_fallback(country: str | None) -> str:
    """The fixed crisis reply for when the AI cannot answer: it always carries the lines."""
    return _CRISIS_FALLBACK[_key(country)]
