import pytest

from backend.ai_wrapper.agents.agent3_empathy_synthesizer.openings import has_mirroring_opening

ECHOES = [
    "It sounds like you're carrying a lot right now.",
    "**It sounds like** this has been heavy.",
    "That sounds incredibly painful.",
    "I hear that you feel alone.",
    "I'm hearing that you feel alone.",
    "You're feeling overwhelmed, and that's okay.",
    "You've been carrying this for a long time.",
    "What I'm hearing is that you miss him.",
    "Sitting with the memory of a final conversation can feel utterly exhausting.",
    "Replaying a last call can be so heavy.",
    "Grief rarely arrives as a single, neat emotion.",
    "There is no manual or script for standing in the waiting room of a loss.",
    "When a long illness finally ends, the body often exhales.",
    "Many people feel relieved after a long caring season.",
]

NATURAL = [
    "I'm so sorry about your grandmother, and I'm really glad you told me.",
    "I understand, and you're not alone in this.",
    "I can only imagine how hard that is, and I'm here with you.",
    "I hear how exhausted you are, and I want to help.",
    "It makes sense that you feel this way, and I'm here.",
    "I'm so sorry about your dad.",
    "Losing someone this way leaves a lot of questions, and you don't have to answer any of them today.",
    "Here's a gentle way to start tonight.",
    "You don't have to hold all of this alone.",
    "Caring for someone with dementia is exhausting in ways few people see.",
    "I'm here, and I'm glad you wrote.",
]


@pytest.mark.parametrize("text", ECHOES)
def test_echoing_or_impersonal_openings_are_flagged(text):
    assert has_mirroring_opening(text)


@pytest.mark.parametrize("text", NATURAL)
def test_warm_personal_openings_pass(text):
    assert not has_mirroring_opening(text)
