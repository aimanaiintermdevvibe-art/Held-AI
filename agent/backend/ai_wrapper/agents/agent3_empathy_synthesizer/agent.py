from backend.ai_wrapper.agents.common import BaseAgent
from backend.ai_wrapper.agents.common.serialization import to_json
from backend.ai_wrapper.contracts import ClinicalAnalysis, GriefAnalysis
from backend.ai_wrapper.memory import MEMORY_NOTE
from backend.grief.note_tools import DATA_NOTICE, TOOL_DECLARATIONS, NoteTools
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
        notes: list[dict] | None = None,
        note_tools: NoteTools | None = None,
        is_note: bool = False,
    ) -> str:
        """note_tools offers the read-only grief note tools (the model decides whether to call
        them); notes are the results of tools already called for this message, given as data."""
        payload = {
            "message": message,
            "analysis": analysis.model_dump() if analysis else None,
            "mode": mode,
            "revision_feedback": audit_feedback or [],
        }
        if is_note:
            payload["is_note"] = True  # message is a grief note sent from the app, not a question
        if user_context:
            payload["user_context"] = user_context
        if notes:
            payload["grief_note_tool_results"] = {"notice": DATA_NOTICE, "results": notes}
        if note_tools or notes:
            payload["today"] = (note_tools.today if note_tools else _today()).isoformat()
        if memory:
            payload["conversation_memory"] = memory

        system_prompt = SYSTEM_PROMPT
        if memory:
            system_prompt += (MEMORY_NOTE.replace("latest_prompt", "message"))

        if note_tools:
            return await self.client.generate_text_with_tools(
                model=self.model,
                system=system_prompt,
                user_input=to_json(payload),
                tools=TOOL_DECLARATIONS,
                call_tool=note_tools.call,
                thinking_level=self.thinking_level,
                api_key=self.api_key,
            )
        return await self.client.generate_text(
            model=self.model,
            system=system_prompt,
            user_input=to_json(payload),
            thinking_level=self.thinking_level,
            api_key=self.api_key,
        )


def _today():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date()
