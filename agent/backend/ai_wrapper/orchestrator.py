from backend.ai_wrapper.agents.agent1_intent_guardrail import IntentGuardrailAgent
from backend.ai_wrapper.agents.agent1_intent_guardrail.policy import has_immediate_crisis_language
from backend.ai_wrapper.agents.agent2_clinical_reasoner import ClinicalReasoningAgent
from backend.ai_wrapper.agents.agent3_empathy_synthesizer import EmpathySynthesizerAgent
from backend.ai_wrapper.agents.agent3_empathy_synthesizer.openings import (
    OPENING_FEEDBACK,
    STOCK_OPENING_FEEDBACK,
    has_mirroring_opening,
    has_stock_opening,
)
from backend.ai_wrapper.agents.agent4_compliance_auditor import ComplianceAuditorAgent
from backend.ai_wrapper.client import GeminiClient
from backend.ai_wrapper.memory import (
    REASONING_MESSAGES,
    SAFETY_CHECK_IN_FEEDBACK,
    audit_view,
    current_chat,
    repeats_safety_check_in,
    routing_view,
)
from backend.ai_wrapper.profile import user_context
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

# Agent 1's category -> the reply mode Agents 2-4 work in.
MODES = {
    "CRISIS": "crisis",
    "OFF_TOPIC": "off_topic",
    "GRIEF_WORKBOOK": "grief",
    "CLINICAL_QUERY": "clinical_support",
    "GENERAL_SUPPORT": "general_support",
}


# Spaces a model sometimes emits that render oddly in a chat: no-break and thin spaces become
# ordinary spaces, zero-width spaces are dropped. Zero-width joiners, which Urdu and Persian
# text needs, are kept.
_ODD_SPACES = str.maketrans({
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "​": None, "⁠": None, "﻿": None,
})


def _clean(text: str) -> str:
    return text.translate(_ODD_SPACES).strip()


class AgenticPipeline:
    def __init__(self, client: GeminiClient, settings: Settings) -> None:
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
        # The auditor only checks facts, so it gets the user's side of the memory.
        review_memory = audit_view(memory)
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

            # A set phrase ("I'm so sorry to hear...") instead of engaging with the message. Crisis
            # replies keep theirs: "I'm really glad you told me" is good practice there.
            if attempt < MAX_ATTEMPTS - 1 and mode != "crisis" and has_stock_opening(draft):
                feedback = [STOCK_OPENING_FEEDBACK]
                continue

            if attempt < MAX_ATTEMPTS - 1 and mode != "crisis" and repeats_safety_check_in(draft, memory):
                feedback = [SAFETY_CHECK_IN_FEEDBACK]
                continue

            audit = await self.audit_agent.run(
                draft=draft, answer_mode=mode, memory=review_memory, user_context=context, documents=documents,
                notes=notes,
            )
            if audit.compliant and audit.final_text.strip():
                return _clean(audit.final_text)

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
        unlike the mode they are also used for off-topic questions.

        Each agent gets only the memory it needs: the router the user's messages and the last
        few turns of this chat, the reasoner a few more, the writer everything, and the auditor
        the user's side."""
        context = user_context(profile)
        documents = documents or None
        notes = notes or None  # the user's grief notes, only when the message refers to them
        reply = dict(memory=memory, context=context, documents=documents, notes=notes)
        if has_immediate_crisis_language(prompt):
            return await self._respond(prompt, mode="crisis", **reply)

        # Routing never sees other chats, so an old topic cannot turn a new message into a crisis.
        intent = await self.intent_agent.run(message=prompt, memory=routing_view(memory))
        mode = MODES[intent.category]

        if mode == "grief":
            analysis = await self.reasoning_agent.run_grief(
                message=prompt, memory=current_chat(memory, REASONING_MESSAGES),
            )
            return await self._respond(prompt, mode=mode, analysis=analysis, **reply)
        if mode == "clinical_support":
            analysis = await self.reasoning_agent.run(
                message=prompt, memory=current_chat(memory, REASONING_MESSAGES), user_context=context,
                documents=documents,
            )
            return await self._respond(prompt, mode=mode, analysis=analysis, **reply)
        return await self._respond(prompt, mode=mode, **reply)
