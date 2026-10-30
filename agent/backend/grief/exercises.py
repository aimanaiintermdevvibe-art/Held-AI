"""Guided grief-workbook exercises a note can answer. Keys are stable; stored with the note."""

EXERCISES = [
    {"key": "letter_to_loved_one", "title": "A letter to my loved one",
     "prompt": "Write to them directly. What would you like them to know today?"},
    {"key": "memory_to_keep", "title": "A memory I want to keep",
     "prompt": "Describe a moment with them in as much detail as you can: where you were, what you heard, what you felt."},
    {"key": "unsaid_words", "title": "What I wish I had said",
     "prompt": "Is there something you didn't get to say, or wish you had said differently?"},
    {"key": "feelings_today", "title": "How I'm feeling today",
     "prompt": "Name what you're feeling right now, even if it's mixed or confusing. Where do you notice it in your body?"},
    {"key": "what_helps", "title": "What helps me cope",
     "prompt": "What has brought you even a little comfort lately? People, places, routines or small things."},
    {"key": "hard_dates", "title": "Hard dates ahead",
     "prompt": "Are there anniversaries, birthdays or holidays coming up? How would you like to spend them?"},
    {"key": "my_support", "title": "Who supports me",
     "prompt": "Who can you lean on right now? Is there someone you would like to reach out to?"},
    {"key": "things_im_carrying", "title": "Things I'm carrying",
     "prompt": "Is there guilt, anger or regret weighing on you? Write it down without judging it."},
    {"key": "carrying_them_forward", "title": "Carrying them with me",
     "prompt": "What part of them, a value, habit, phrase or lesson, would you like to carry forward?"},
]
EXERCISES_BY_KEY = {exercise["key"]: exercise for exercise in EXERCISES}


def exercise_summary(key: str | None) -> dict | None:
    """{key, title} for a stored exercise key, or None."""
    exercise = EXERCISES_BY_KEY.get(key or "")
    return {"key": exercise["key"], "title": exercise["title"]} if exercise else None
