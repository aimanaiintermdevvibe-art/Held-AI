from backend.ai_wrapper.contracts import IntentDecision
from backend.ai_wrapper.memory import MEMORY_NOTE, with_memory
from .prompt import SYSTEM_PROMPT
from .schemas import ExecutionPlan


class Supervisor:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings

    async def create_plan(
        self, *, prompt: str, intent: IntentDecision, memory: dict | None = None,
    ) -> ExecutionPlan:
        try:
            return await self.client.generate_structured(
                model=self.settings.gemini_agent1_model,
                system=SYSTEM_PROMPT + (MEMORY_NOTE if memory else ""),
                user_input=with_memory(prompt, memory),
                response_model=ExecutionPlan,
                thinking_level=self.settings.gemini_agent1_thinking_level,
                api_key=self.settings.gemini_agent1_api_key or None,
            )
        except (RuntimeError, ValueError, TimeoutError):
            return ExecutionPlan(
                intent="GRIEF" if intent.category == "GRIEF_WORKBOOK" else intent.category
            )