import pytest

from backend.ai_wrapper.agents.agent3_empathy_synthesizer.openings import has_mirroring_opening, has_stock_opening

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


SPECIFIC = [
    "Four nights in a row, straight after covering extra hours: no wonder you're dreading it.",
    "Twelve years with Max. No wonder the house feels so quiet.",
    "Getting the speech to sound like you is the hard part, and it's fixable.",
    "Your sister's wedding is on 14 December.",
    "Losing someone this way leaves a lot of questions, and you don't have to answer any of them today.",
    "Here's a gentle way to start tonight.",
    "Snapping at Tom after a night shift doesn't make you a bad partner.",
    "Depression is usually diagnosed when symptoms last at least two weeks.",
]


@pytest.mark.parametrize("text", ECHOES)
def test_echoing_or_impersonal_openings_are_flagged(text):
    assert has_mirroring_opening(text)


@pytest.mark.parametrize("text", NATURAL + SPECIFIC)
def test_warm_personal_openings_pass(text):
    assert not has_mirroring_opening(text)


STOCK = [
    "I'm so sorry to hear about your grandmother.",
    "I'm really glad you told me.",
    "I am truly sorry you're going through this.",
    "**I'm so glad** you reached out.",
    "Thank you for sharing this with me.",
    "Thanks for telling me.",
    "I completely understand how heavy that is.",
    "I hear you, and that matters.",
    "I'm here with you.",
    "I can only imagine how hard that is.",
    "You're not alone in this.",
    "That's so hard.",
    "Oh, I'm so sorry.",
    "What a lot to carry.",
]



@pytest.mark.parametrize("text", STOCK)
def test_stock_sympathy_openings_are_flagged(text):
    assert has_stock_opening(text)


@pytest.mark.parametrize("text", SPECIFIC + ECHOES)
def test_specific_openings_are_not_stock(text):
    assert not has_stock_opening(text)
