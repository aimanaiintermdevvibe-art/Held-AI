from typing import Literal

from pydantic import BaseModel, Field


IntentCategory = Literal["CLINICAL_QUERY", "GRIEF_WORKBOOK", "CRISIS", "OFF_TOPIC"]

class IntentDecision(BaseModel):
    category: IntentCategory
    reasoning: str = Field(description="A short, non-sensitive routing explanation")

class ClinicalAnalysis(BaseModel):
    """Structured reading of a mental-health concern. Never used for grief."""

    analysis_mode: Literal["clinical_support"]
    matched_criteria: list[str]
    timeline_assessment: str
    ruleouts: list[str]
    clinical_key_points: list[str]
    primary_emotions: list[str]
    recommended_response_angle: str

class GriefAnalysis(BaseModel):
    """What a grief companion needs to know. Deliberately has no criteria, rule-outs or
    diagnostic fields: grief is a response to loss, not an illness to assess."""

    analysis_mode: Literal["grief"]
    loss_context: str = Field(description="Who or what was lost and the circumstances the user actually shared; 'not stated' if unknown")
    emotions_present: list[str] = Field(description="Feelings the user expressed, including mixed ones such as guilt, relief, anger or numbness")
    details_to_honour: list[str] = Field(description="Specific memories, names or words the user shared that a caring reply should acknowledge")
    companioning_angle: str = Field(description="How to be with this person right now, e.g. witness, invite a memory, make room for guilt")
    avoid: list[str] = Field(description="Things that would feel dismissive or clinical for this person")

class AuditDecision(BaseModel):
    compliant: bool
    final_text: str
    issues: list[str]
