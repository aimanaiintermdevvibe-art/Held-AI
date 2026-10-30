from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.grief.exercises import EXERCISES_BY_KEY


class _NoteFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # A mood by Held id (moodId) or by code (mood, e.g. "sad"); see GET /agentic/grief-notes/moods.
    moodId: Optional[str] = Field(default=None, max_length=64)
    mood: Optional[str] = Field(default=None, max_length=64)
    exercise: Optional[str] = Field(default=None, description="Workbook exercise key, or null to clear it")

    @model_validator(mode="after")
    def _known_exercise(self):
        if self.exercise is not None and self.exercise not in EXERCISES_BY_KEY:
            raise ValueError(f"Unknown exercise {self.exercise!r}; see GET /agentic/grief-notes/exercises.")
        if self.moodId and self.mood:
            raise ValueError("Send either moodId or mood, not both.")
        return self


class GriefNoteCreate(_NoteFields):
    date: date
    content: str = Field(min_length=1, max_length=5000)  # Held's limit

    @model_validator(mode="after")
    def _needs_mood(self):
        if not (self.moodId or self.mood):
            raise ValueError("A mood is required (moodId or mood).")
        return self


class GriefNoteUpdate(_NoteFields):
    content: Optional[str] = Field(default=None, min_length=1, max_length=5000)

    @model_validator(mode="after")
    def _needs_a_change(self):
        if not (self.content or self.moodId or self.mood or "exercise" in self.model_fields_set):
            raise ValueError("Send content, a mood, or exercise.")
        return self
