from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _NoteFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # A mood by Held id (moodId) or by code (mood, e.g. "sad"); see GET /agentic/grief-notes/moods.
    moodId: Optional[str] = Field(default=None, max_length=64)
    mood: Optional[str] = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _one_mood(self):
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
        if not (self.content or self.moodId or self.mood):
            raise ValueError("Send content or a mood.")
        return self
