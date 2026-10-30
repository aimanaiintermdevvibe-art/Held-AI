from backend.ai_wrapper.agents.common import BaseAgent
from backend.ai_wrapper.agents.common.serialization import to_json
from backend.ai_wrapper.contracts import ClinicalAnalysis, GriefAnalysis
from backend.ai_wrapper.memory import MEMORY_NOTE
from .prompt import SYSTEM_PROMPT


class EmpathySynthesizerAgent(BaseAgent):
    name = "agent3_empathy_synthesizer"
    label = "Empathy & Persona Synthesizer"

    async def run(
        self, *,
        message: str,
        analysis: ClinicalAnalysis | GriefAnalysis | None = None,
        mode: str = "general_support",
        audit_feedback: list[str] | None = None,
        memory: dict | None = None,
        user_context: dict | None = None,
        documents: list[dict] | None = None,
        notes: list[dict] | None = None,
    ) -> str:
        payload = {
            "message": message,
            "analysis": analysis.model_dump() if analysis else None,
            "mode": mode,
            "revision_feedback": audit_feedback or [],
        }
        if user_context:
            payload["user_context"] = user_context
        if documents:
            payload["document_excerpts"] = documents
        if notes:
            payload["grief_notes"] = notes
        if memory:
            payload["conversation_memory"] = memory

        system_prompt = SYSTEM_PROMPT
        if memory:
            system_prompt += (MEMORY_NOTE.replace("latest_prompt", "message"))

        return await self.client.generate_text(
            model=self.model,
            system=system_prompt,
            user_input=to_json(payload),
            thinking_level=self.thinking_level,
            api_key=self.api_key,
        )
