from backend.ai_wrapper.agents.agent1_intent_guardrail import IntentGuardrailAgent
from backend.ai_wrapper.agents.agent1_intent_guardrail.policy import has_immediate_crisis_language
from backend.ai_wrapper.agents.agent2_clinical_reasoner import ClinicalReasoningAgent
from backend.ai_wrapper.agents.agent3_empathy_synthesizer import EmpathySynthesizerAgent
from backend.ai_wrapper.agents.agent3_empathy_synthesizer.openings import (
    OPENING_FEEDBACK,
    has_mirroring_opening,
)
from backend.ai_wrapper.agents.agent4_compliance_auditor import ComplianceAuditorAgent
from backend.ai_wrapper.client import GeminiClient
from backend.ai_wrapper.contracts import IntentDecision
from backend.ai_wrapper.memory import without_previous
from backend.ai_wrapper.profile import user_context
from backend.ai_wrapper.supervisor import ExecutionPlan, Supervisor
from backend.core.config import Settings

SAFE_RESPONSE = (
    "I'm sorry, I wasn't able to put together a good reply just now. "
    "I'm still here to help — would you try sending that again or telling me more?"
)
OFF_TOPIC_FALLBACK_RESPONSE = (
    "I'm sorry, I couldn't put together a good answer to that just now. "
    "Could you try asking again in a slightly different way?"
)
CRISIS_FALLBACK_RESPONSE = (
    "I'm really glad you reached out, and I'm staying right here with you. "
    "If you are in immediate danger, please contact local emergency services or visit "
    "https://findahelpline.com right now."
)

MAX_ATTEMPTS = 2


class AgenticPipeline:
    def __init__(self, client: GeminiClient, settings: Settings) -> None:
        self.supervisor = Supervisor(client, settings)
        self.intent_agent = IntentGuardrailAgent(
            client, settings.gemini_agent1_model, settings.gemini_agent1_thinking_level, settings.gemini_agent1_api_key or None
        )
        self.reasoning_agent = ClinicalReasoningAgent(
            client, settings.gemini_agent2_model, settings.gemini_agent2_thinking_level, settings.gemini_agent2_api_key or None
        )
        self.empathy_agent = EmpathySynthesizerAgent(
            client, settings.gemini_agent3_model, settings.gemini_agent3_thinking_level, settings.gemini_agent3_api_key or None
        )
        self.audit_agent = ComplianceAuditorAgent(
            client, settings.gemini_agent4_model, settings.gemini_agent4_thinking_level, settings.gemini_agent4_api_key or None
        )

    async def _respond(
        self, prompt: str, *, mode: str = "general_support", analysis=None, memory=None, context=None,
        documents=None, notes=None,
    ) -> str:
        if mode == "off_topic":
            context = None  # a general question is answered without the user's care situation
        feedback: list[str] = []
        for attempt in range(MAX_ATTEMPTS):
            draft = (await self.empathy_agent.run(
                message=prompt,
                analysis=analysis,
                mode=mode,
                audit_feedback=feedback,
                memory=memory,
                user_context=context,
                documents=documents,
                notes=notes,
            )).strip()
            
            if not draft:
                feedback = ["The previous attempt was empty. Write a complete, warm response."]
                continue
                
            if attempt < MAX_ATTEMPTS - 1 and has_mirroring_opening(draft):
                feedback = [OPENING_FEEDBACK]
                continue
                
            audit = await self.audit_agent.run(
                draft=draft, answer_mode=mode, memory=memory, user_context=context, documents=documents,
                notes=notes,
            )
            if audit.compliant and audit.final_text.strip():
                return audit.final_text.strip()
                
            feedback = audit.issues or ["Safety review failed. Revise into a safe response."]
            
        return self._fallback(mode)

    @staticmethod
    def _fallback(mode: str) -> str:
        if mode == "crisis":
            return CRISIS_FALLBACK_RESPONSE
        if mode == "off_topic":
            return OFF_TOPIC_FALLBACK_RESPONSE
        return SAFE_RESPONSE

    async def run(
        self, *, prompt: str, memory: dict | None = None, profile: dict | None = None,
        documents: list[dict] | None = None,
        notes: list[dict] | None = None,
    ) -> str:
        """profile is the user's Held profile; its onboarding answers select one of the four
        user modes (myself/caregiver x diagnosed/undiagnosed) for reasoning, writing and review.
        documents are passages retrieved from the user's uploaded documents (document_excerpts);
        unlike the mode they are also used for off-topic questions."""
        context = user_context(profile)
        documents = documents or None
        notes = notes or None  # the user's grief notes, only when the message refers to them
        if has_immediate_crisis_language(prompt):
            return await self._respond(prompt, mode="crisis", memory=memory, context=context, documents=documents, notes=notes)

        routing_memory = without_previous(memory)
        intent = await self.intent_agent.run(message=prompt, memory=routing_memory)
        
        if intent.category == "CRISIS":
            return await self._respond(prompt, mode="crisis", memory=memory, context=context, documents=documents, notes=notes)
        if intent.category == "OFF_TOPIC":
            return await self._respond(prompt, mode="off_topic", memory=memory, context=context, documents=documents, notes=notes)

        plan = await self.supervisor.create_plan(prompt=prompt, intent=intent, memory=routing_memory)
        route = plan.intent if plan.intent == "CRISIS" else (
            "GRIEF" if intent.category == "GRIEF_WORKBOOK" else plan.intent
        )

        if route == "CRISIS":
            return await self._respond(prompt, mode="crisis", memory=memory, context=context, documents=documents, notes=notes)
        if route == "OFF_TOPIC":
            return await self._respond(prompt, mode="off_topic", memory=memory, context=context, documents=documents, notes=notes)
        if route == "GRIEF":
            analysis = await self.reasoning_agent.run_grief(message=prompt, memory=routing_memory)
            return await self._respond(prompt, mode="grief", analysis=analysis, memory=memory, context=context, documents=documents, notes=notes)
        if route == "CLINICAL_QUERY":
            analysis = await self.reasoning_agent.run(
                message=prompt, memory=routing_memory, user_context=context, documents=documents,
            )
            return await self._respond(prompt, mode="clinical_support", analysis=analysis, memory=memory, context=context, documents=documents, notes=notes)

        return await self._respond(prompt, mode="general_support", memory=memory, context=context, documents=documents, notes=notes)