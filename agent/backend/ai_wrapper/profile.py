"""The user's care mode, from the onboarding answers in their Held profile.

Onboarding asks two questions, which give four modes:
- who the user is chatting for: themselves (role SELF) or someone they care for (CAREGIVER)
- whether that person has a diagnosis: Held stores the selected conditions in `diseases`;
  an empty list means no diagnosis.
"""

MODES = ("myself_diagnosed", "myself_undiagnosed", "caregiver_diagnosed", "caregiver_undiagnosed")
RELATION_NAMES = {
    "PARENT": "parent",
    "CHILD": "child",
    "PARTNER": "partner",
    "SIBLING": "sibling",
    "FRIEND": "friend",
}


def user_context(profile: dict | None) -> dict | None:
    """Prompt-ready facts about who the user is chatting for; None until they have onboarded."""
    if not profile or profile.get("role") not in ("SELF", "CAREGIVER"):
        return None
    caregiver = profile["role"] == "CAREGIVER"
    conditions = [
        d["name"].strip() for d in profile.get("diseases") or []
        if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"].strip()
    ]
    context: dict = {
        "mode": f"{'caregiver' if caregiver else 'myself'}_{'diagnosed' if conditions else 'undiagnosed'}",
        "chatting_for": "someone they care for" if caregiver else "themselves",
    }
    if caregiver:
        relation = profile.get("relation")
        other = profile.get("relationOther")
        if relation == "OTHER" and isinstance(other, str) and other.strip():
            context["cares_for"] = other.strip()[:50]
        else:
            context["cares_for"] = RELATION_NAMES.get(relation, "someone close to them")
    if conditions:
        key = "cared_for_person_diagnosed_conditions" if caregiver else "user_diagnosed_conditions"
        context[key] = conditions
    else:
        context["diagnosis"] = "none on record"
    return context


def describe(context: dict | None) -> str:
    """One-line, human-readable summary of a user_context (for the terminal client)."""
    if not context:
        return "Not set up yet"
    if context["mode"].startswith("caregiver"):
        who = f"Caregiver for your {context['cares_for']}" if context["cares_for"] in RELATION_NAMES.values() \
            else f"Caregiver for {context['cares_for']}"
        conditions = context.get("cared_for_person_diagnosed_conditions")
    else:
        who = "Myself"
        conditions = context.get("user_diagnosed_conditions")
    return f"{who} · " + (f"Diagnosed: {', '.join(conditions)}" if conditions else "No diagnosis")
