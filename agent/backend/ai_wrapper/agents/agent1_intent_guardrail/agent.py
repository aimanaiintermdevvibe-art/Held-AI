from backend.ai_wrapper.agents.common import BaseAgent
from backend.ai_wrapper.contracts import IntentDecision
from backend.ai_wrapper.memory import MEMORY_NOTE, with_memory
from .prompt import SYSTEM_PROMPT
from .policy import has_immediate_crisis_language


class IntentGuardrailAgent(BaseAgent):
    name = "agent1_intent_guardrail"
    label = "Intent & Safety Guardrail"

    async def run(self, *, message: str, memory: dict | None = None) -> IntentDecision:
        if has_immediate_crisis_language(message):
            return IntentDecision(category="CRISIS", reasoning="Local safety check.")
        return await self.client.generate_structured(
            model=self.model, 
            system=SYSTEM_PROMPT + (MEMORY_NOTE if memory else ""),
            user_input=with_memory(message, memory),
            response_model=IntentDecision, 
            thinking_level=self.thinking_level,
            api_key=self.api_key,
        )