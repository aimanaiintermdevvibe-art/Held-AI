from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class DocumentSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    document_ids: Optional[List[str]] = Field(default=None, max_length=50)
