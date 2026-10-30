import pytest

from backend.ai_wrapper.memory import with_memory
from backend.ai_wrapper.profile import describe, user_context

DEPRESSION = {"id": "d1", "name": "Depression", "abbreviation": None}
ADHD = {"id": "d2", "name": "Attention-Deficit/Hyperactivity Disorder", "abbreviation": "ADHD"}


@pytest.mark.parametrize("profile, expected", [
    (
        {"role": "SELF", "relation": None, "diseases": [DEPRESSION]},
        {"mode": "myself_diagnosed", "chatting_for": "themselves", "user_diagnosed_conditions": ["Depression"]},
    ),
    (
        {"role": "SELF", "relation": None, "diseases": []},
        {"mode": "myself_undiagnosed", "chatting_for": "themselves", "diagnosis": "none on record"},
    ),
    (
        {"role": "CAREGIVER", "relation": "CHILD", "diseases": [ADHD, DEPRESSION]},
        {
            "mode": "caregiver_diagnosed", "chatting_for": "someone they care for", "cares_for": "child",
            "cared_for_person_diagnosed_conditions": ["Attention-Deficit/Hyperactivity Disorder", "Depression"],
        },
    ),
    (
        {"role": "CAREGIVER", "relation": "PARENT", "diseases": []},
        {
            "mode": "caregiver_undiagnosed", "chatting_for": "someone they care for", "cares_for": "parent",
            "diagnosis": "none on record",
        },
    ),
])
def test_onboarding_answers_select_one_of_four_modes(profile, expected):
    assert user_context(profile) == expected


def test_other_relation_uses_the_users_own_words():
    context = user_context({"role": "CAREGIVER", "relation": "OTHER", "relationOther": " Neighbour ", "diseases": []})
    assert context["cares_for"] == "Neighbour"


def test_caregiver_without_relation_is_still_a_caregiver():
    context = user_context({"role": "CAREGIVER", "relation": None, "diseases": None})
    assert context["mode"] == "caregiver_undiagnosed"
    assert context["cares_for"] == "someone close to them"


@pytest.mark.parametrize("profile", [None, {}, {"role": None, "onboardingComplete": False}, {"role": "ADMIN"}])
def test_no_mode_before_onboarding(profile):
    assert user_context(profile) is None


def test_malformed_diseases_are_ignored():
    context = user_context({"role": "SELF", "diseases": [{"name": "  "}, "Depression", {"id": "x"}, DEPRESSION]})
    assert context["user_diagnosed_conditions"] == ["Depression"]


def test_describe_summarises_the_mode():
    assert describe(None) == "Not set up yet"
    assert describe(user_context({"role": "SELF", "diseases": []})) == "Myself · No diagnosis"
    assert describe(user_context({"role": "CAREGIVER", "relation": "PARENT", "diseases": [DEPRESSION]})) == (
        "Caregiver for your parent · Diagnosed: Depression"
    )


def test_agent_input_carries_the_mode_with_or_without_memory():
    context = user_context({"role": "SELF", "diseases": []})
    assert with_memory("hi", None) == "hi"
    assert '"user_context"' in with_memory("hi", None, context)
    combined = with_memory("hi", {"summary": None, "recent_messages": []}, context)
    assert combined.index('"user_context"') < combined.index('"conversation_memory"') < combined.index('"latest_prompt"')
