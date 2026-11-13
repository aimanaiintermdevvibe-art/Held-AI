from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class ChatSessionResponse(BaseModel):
    id: str
    user_id: Optional[str] = None
    title: str
    status: str = "active"
    metadata: Optional[Dict[str, Any]] = None
    session_summary: Optional[str] = None
    last_summarized_message_id: Optional[str] = None
    total_message_count: Optional[int] = 0
    message_count: Optional[int] = 0
    last_message_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ChatMessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[str] = None


class SessionDetailResponse(BaseModel):
    session: ChatSessionResponse
    messages: List[ChatMessageResponse] = []
