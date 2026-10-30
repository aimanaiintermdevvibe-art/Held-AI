from typing import Any, Dict, List, Optional
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


class AgenticRunResponse(BaseModel):
    response: str
    session_id: Optional[str] = None
    session: Optional[Dict[str, Any]] = None
    # Passages from the user's uploaded documents that this reply was given (RAG).
    sources: Optional[List[Dict[str, Any]]] = None