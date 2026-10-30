from backend.ai_wrapper.agents.common import BaseAgent
from backend.ai_wrapper.contracts import ClinicalAnalysis, GriefAnalysis
from backend.ai_wrapper.memory import MEMORY_NOTE, with_memory
from .prompt import GRIEF_SYSTEM_PROMPT, SYSTEM_PROMPT


class ClinicalReasoningAgent(BaseAgent):
    name = "agent2_clinical_reasoner"
    label = "Clinical & Grief Reasoning Engine"

    async def run(
        self, *, message: str, memory: dict | None = None, user_context: dict | None = None,
        documents: list[dict] | None = None,
    ) -> ClinicalAnalysis:
        """Clinical reading of a mental-health or caregiving concern."""
        return await self.client.generate_structured(
            model=self.model,
            system=SYSTEM_PROMPT + (MEMORY_NOTE if memory else ""),
            user_input=with_memory(message, memory, user_context, documents),
            response_model=ClinicalAnalysis,
            thinking_level=self.thinking_level,
            api_key=self.api_key,
        )

    async def run_grief(self, *, message: str, memory: dict | None = None) -> GriefAnalysis:
        """Grief mode: a companioning read of a loss, with no clinical fields."""
        return await self.client.generate_structured(
            model=self.model,
            system=GRIEF_SYSTEM_PROMPT + (MEMORY_NOTE if memory else ""),
            user_input=with_memory(message, memory),
            response_model=GriefAnalysis,
            thinking_level=self.thinking_level,
            api_key=self.api_key,
        )
