from typing import Literal

from pydantic import BaseModel, ConfigDict

class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["CLINICAL_QUERY", "GRIEF", "GENERAL_SUPPORT", "CRISIS", "OFF_TOPIC"]

    @property
    def needs_reasoning(self) -> bool:
        return self.intent in {"CLINICAL_QUERY", "GRIEF"}