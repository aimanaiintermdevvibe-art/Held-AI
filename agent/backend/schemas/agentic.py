from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field


class AgenticRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=10000)
    session_id: Optional[str] = Field(default=None)
    user_id: Optional[str] = Field(default=None)
    title: Optional[str] = Field(default=None, max_length=200)
    metadata: Optional[Dict[str, Any]] = Field(default=None)
    session: Optional[Dict[str, Any]] = Field(default=None)
    session_data: Optional[Dict[str, Any]] = Field(default=None)
    # True when prompt is a grief note the user wrote in the app and sent to the agent. A note always
    # starts a new chat, and the chat and the note's message are marked isNote in their metadata.
    isNote: bool = Field(default=False)


class AgenticRunResponse(BaseModel):
    response: str
    session_id: Optional[str] = None
    session: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None  # this reply's token use (SHOW_TOKEN_USAGE)
