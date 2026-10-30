from abc import ABC, abstractmethod
from typing import Any

from backend.ai_wrapper.client import GeminiClient


class BaseAgent(ABC):
    name: str
    label: str

    def __init__(
        self,
        client: GeminiClient,
        model: str,
        thinking_level: str,
        api_key: str | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.thinking_level = thinking_level
        self.api_key = api_key

    @abstractmethod
    async def run(self, **kwargs: Any) -> Any:
        raise NotImplementedError