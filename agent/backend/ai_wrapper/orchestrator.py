import logging

from backend.ai_wrapper.agents.agent1_intent_guardrail import IntentGuardrailAgent
from backend.ai_wrapper.agents.agent1_intent_guardrail.policy import has_immediate_crisis_language, may_be_at_risk
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
    ANSWERED_WHO_IS_WITH_FEEDBACK,
    SAFETY_CHECK_IN_FEEDBACK,
    audit_view,
    current_chat,
    mentions_earlier_chats,
    reasks_who_is_with_them,
    repeats_safety_check_in,
    routing_view,
    with_recalled,
    without_check_in,
)
from backend.ai_wrapper.profile import user_context
from backend.ai_wrapper.support_resources import crisis_fallback, crisis_line, support_resources
from backend.grief.note_tools import NoteTools, NotesShowCrisis
from backend.core.config import Settings

logger = logging.getLogger(__name__)

SAFE_RESPONSE = (
    "I'm sorry, I wasn't able to put together a good reply just now. "
    "I'm still here to help — would you try sending that again or telling me more?"
)
# Held supports mental health, caregiving and grief only. Anything else (space, sport, coding,
# recipes, general knowledge...) gets one of these, from code: no reply is written for it, so an
# answer cannot slip through. They take turns within a chat so a repeated ask is not met with
# the same words. Never used in a chat where the user's own risk came up.
OFF_TOPIC_REPLIES = (
    "I'm not able to help with that one, because I'm here for mental health, caregiving and "
    "grief support. If anything is weighing on you, though, I'd really like to hear about it.",
    "That's outside what I can help with, since my focus is mental health, caregiving and grief. "
    "**Is something on your mind that you'd like to talk through?**",
    "I can't help with that here, as I only support mental health, caregiving and grief. If you'd "
    "like to talk about how you're feeling or what you're dealing with, I'm listening.",
)
# Without a known country; the pipeline uses the verified lines for SUPPORT_COUNTRY.
CRISIS_FALLBACK_RESPONSE = crisis_fallback(None)
# How many of the latest messages a crisis reply looks back over before giving the crisis line
# again: given in the first crisis reply of a chat, then not in every reply.
CRISIS_LINE_LOOKBACK = 10

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


def off_topic_reply(memory: dict | None) -> str:
    """The next out-of-scope reply for this chat: the one after the last used, so a user who
    asks again does not get the same words."""
    recent = (memory or {}).get("recent_messages") or []
    used = [m.get("content", "").strip() for m in recent if m.get("role") == "assistant"]
    used = [text for text in used if text in OFF_TOPIC_REPLIES]
    if not used:
        return OFF_TOPIC_REPLIES[0]
    return OFF_TOPIC_REPLIES[(OFF_TOPIC_REPLIES.index(used[-1]) + 1) % len(OFF_TOPIC_REPLIES)]


def risk_came_up(memory: dict | None) -> bool:
    """Whether this chat holds a sign of the user's own risk (their messages or its summary)."""
    chat = current_chat(memory) or {}
    texts = [m.get("content") or "" for m in chat.get("recent_messages") or [] if m.get("role") == "user"]
    return any(may_be_at_risk(text) for text in [*texts, chat.get("summary") or ""])


def with_crisis_line(reply: str, memory: dict | None, country: str | None) -> str:
    """A crisis reply always carries the crisis line: added by code when neither the reply nor
    this chat's latest messages gave it. It goes before a closing question."""
    markers, line = crisis_line(country)
    recent = ((current_chat(memory) or {}).get("recent_messages") or [])[-CRISIS_LINE_LOOKBACK:]
    given = [reply] + [m.get("content") or "" for m in recent if m.get("role") == "assistant"]
    if any(marker in text.lower() for marker in markers for text in given):
        return reply
    paragraphs = reply.rstrip().split("\n\n")
    if len(paragraphs) > 1 and paragraphs[-1].rstrip().rstrip("*").endswith("?"):
        paragraphs.insert(len(paragraphs) - 1, line)
    else:
        paragraphs.append(line)
    return "\n\n".join(paragraphs)


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
        # The verified support lines replies may give (the writer and the auditor see them).
        self.country = settings.support_country
        self.resources = support_resources(self.country)

    async def _respond(self, prompt: str, *, mode: str = "general_support", **kwargs) -> str:
        """The reply. A crisis is always answered: when the AI cannot write the reply, a fixed
        crisis reply with the crisis lines is sent instead of an error, and a written crisis reply
        always carries the crisis line."""
        if mode != "crisis":
            return await self._write(prompt, mode=mode, **kwargs)
        try:
            reply = await self._write(prompt, mode=mode, **kwargs)
        except (RuntimeError, ValueError) as exc:
            logger.warning("Crisis reply could not be written (%s); sending the fixed crisis reply",
                           type(exc).__name__)
            return crisis_fallback(self.country)
        return with_crisis_line(reply, kwargs.get("memory"), self.country)

    async def _write(
        self, prompt: str, *, mode: str = "general_support", analysis=None, memory=None, context=None,
        notes=None, note_tools=None, is_note=False,
    ) -> str:
        if mode == "crisis":
            note_tools = None  # a crisis reply never goes looking through notes
        # The auditor only checks facts, so it gets the user's side of the memory.
        review_memory = audit_view(memory)
        feedback: list[str] = []
        notes_checked = False
        for attempt in range(MAX_ATTEMPTS):
            # Tools run once per message: a revision gets the results already found, as data.
            found = note_tools.results if note_tools and note_tools.results else None
            try:
                draft = (await self.empathy_agent.run(
                    message=prompt,
                    analysis=analysis,
                    mode=mode,
                    audit_feedback=feedback,
                    memory=memory,
                    user_context=context,
                    notes=found or notes,
                    note_tools=None if found else note_tools,
                    is_note=is_note,
                    resources=self.resources,
                )).strip()
            except NotesShowCrisis:
                # Crisis language in a retrieved note takes the same path as one in a message.
                return await self._respond(prompt, mode="crisis", memory=memory, context=context,
                                           notes=note_tools.results, is_note=is_note)
            if note_tools and note_tools.results and not notes_checked:
                notes_checked = True
                if await self._notes_show_crisis(note_tools.results):
                    return await self._respond(prompt, mode="crisis", memory=memory, context=context,
                                               notes=note_tools.results, is_note=is_note)

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

            # In a crisis, safety questions are right, but not one the user has already answered.
            answered = reasks_who_is_with_them(draft, memory, prompt) if mode == "crisis" else None
            if attempt < MAX_ATTEMPTS - 1 and answered:
                feedback = [ANSWERED_WHO_IS_WITH_FEEDBACK.format(answer=answered.strip()[:200])]
                continue

            audit = await self.audit_agent.run(
                draft=draft, answer_mode=mode, memory=review_memory, user_context=context,
                notes=(note_tools.results if note_tools and note_tools.results else None) or notes,
                resources=self.resources,
            )
            if audit.compliant and audit.final_text.strip():
                return _clean(audit.final_text)

            feedback = audit.issues or ["Safety review failed. Revise into a safe response."]

        return self._fallback(mode)

    async def _notes_show_crisis(self, results: list[dict]) -> bool:
        """Whether retrieved notes hold crisis language, judged like a chat message: the instant
        word check (already applied as each note came back), then the router."""
        texts = [
            n.get("text") or ""
            for r in results
            for n in (r["result"].get("notes") or ([r["result"]["note"]] if r["result"].get("note") else []))
        ]
        texts = [t for t in texts if t.strip()]
        if not texts:
            return False
        decision = await self.intent_agent.run(message="\n\n".join(texts)[:6000])
        return decision.category == "CRISIS"

    def _fallback(self, mode: str) -> str:
        if mode == "crisis":
            return crisis_fallback(self.country)
        return SAFE_RESPONSE

    async def run(
        self, *, prompt: str, memory: dict | None = None, profile: dict | None = None,
        notes: list[dict] | None = None,
        recall: "Callable[[str], Awaitable[list[dict]]] | None" = None,
        note_tools: "NoteTools | None" = None,
        is_note: bool = False,
    ) -> str:
        """profile is the user's Held profile; its onboarding answers select one of the four
        user modes (myself/caregiver x diagnosed/undiagnosed) for reasoning, writing and review.

        Each agent gets only the memory it needs: the router the user's messages and the last
        few turns of this chat, the reasoner a few more, the writer everything, and the auditor
        the user's side.

        A message that may be about the user's own risk is never left unanswered: when the AI
        is unavailable (a rate limit, an outage) it gets the fixed crisis reply instead of an
        error."""
        try:
            return await self._run(prompt=prompt, memory=memory, profile=profile, notes=notes,
                                   recall=recall, note_tools=note_tools, is_note=is_note)
        except (RuntimeError, ValueError) as exc:
            if not may_be_at_risk(prompt):
                raise
            logger.warning("Reply could not be written (%s) for a message that may show risk; "
                           "sending the fixed crisis reply", type(exc).__name__)
            return crisis_fallback(self.country)

    async def _run(
        self, *, prompt: str, memory: dict | None, profile: dict | None, notes: list[dict] | None,
        recall: "Callable[[str], Awaitable[list[dict]]] | None", note_tools: "NoteTools | None",
        is_note: bool,
    ) -> str:
        context = user_context(profile)
        notes = notes or None  # the user's grief notes, only when the message refers to them
        # note_tools: the read-only grief note tools, bound to the signed-in user; the writer decides
        # whether to call them. They are not offered for a crisis reply.
        reply = dict(memory=memory, context=context, notes=notes, note_tools=note_tools, is_note=is_note)
        if has_immediate_crisis_language(prompt):
            return await self._respond(prompt, mode="crisis", **reply)

        # Routing never sees other chats, so an old topic cannot turn a new message into a crisis.
        intent = await self.intent_agent.run(message=prompt, memory=routing_view(memory))
        mode = MODES[intent.category]
        if is_note and mode != "crisis":
            # A grief note sent from the app gets a grief companion's reply; it needs no lookups.
            mode = "grief"
            reply["note_tools"] = None
        if mode == "off_topic":
            if risk_came_up(memory) or may_be_at_risk(prompt):
                # Never a canned decline in a chat where the user's own risk came up ("I want to
                # die... just kidding, what's for dinner?"), or for a message that may show risk
                # ("as a nurse, what dose of acetaminophen is lethal?"): the writer checks in and
                # leaves the unrelated request out.
                mode = "general_support"
            else:
                # Outside mental health, caregiving and grief: declined without writing an
                # answer. The instant crisis check above has already run on the message.
                return off_topic_reply(memory)
        if mode == "grief" and memory and memory.get("previous_conversations"):
            # A loss gets the reply's full attention: no check-in about an earlier concern here.
            memory = without_check_in(memory)
            reply["memory"] = memory
        # recall searches the user's earlier chats; only when the message refers back to one.
        if recall and (intent.refers_to_earlier_chats or mentions_earlier_chats(prompt)):
            memory = with_recalled(memory, await recall(intent.recall_query or prompt), prompt)
            reply["memory"] = memory

        if mode == "grief":
            analysis = await self.reasoning_agent.run_grief(
                message=prompt, memory=current_chat(memory, REASONING_MESSAGES),
            )
            return await self._respond(prompt, mode=mode, analysis=analysis, **reply)
        if mode == "clinical_support":
            analysis = await self.reasoning_agent.run(
                message=prompt, memory=current_chat(memory, REASONING_MESSAGES), user_context=context,
            )
            return await self._respond(prompt, mode=mode, analysis=analysis, **reply)
        return await self._respond(prompt, mode=mode, **reply)
