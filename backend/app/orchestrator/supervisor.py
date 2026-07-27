"""Supervisor: decompose the raw task into an ordered plan (TECH_SPEC 3, step 1)."""

from __future__ import annotations

from typing import Any

from app.orchestrator.base import (
    JSON_ONLY_INSTRUCTION,
    Agent,
    PipelineContext,
    Subtask,
)
from app.orchestrator.llm_client import LLMRequest, MalformedResponseError

MAX_SUBTASKS = 6

SYSTEM_PROMPT = f"""You are the Supervisor in a multi-agent pipeline. You do not \
perform the work yourself. Your only job is to decompose a task into an ordered \
list of subtasks that a generic Worker agent can execute one at a time.

Rules:
- Produce between 1 and {MAX_SUBTASKS} subtasks.
- Each subtask must be independently executable given the outputs of the ones \
before it, and must have a single concrete deliverable.
- Order matters: subtask N may rely on the output of subtasks 1..N-1.
- Do not include review or verification steps; a separate Verifier agent handles \
that.

Return this exact shape:
{{
  "subtasks": [
    {{"index": 1, "agent": "Worker", "description": "..."}}
  ],
  "reasoning": "one or two sentences on why you split it this way"
}}

{JSON_ONLY_INSTRUCTION}"""


class SupervisorAgent(Agent):
    name = "Supervisor"
    purpose = "plan"

    def build_prompt(self, context: PipelineContext, **kwargs: Any) -> LLMRequest:
        return LLMRequest(
            system=SYSTEM_PROMPT,
            user=f"Task to decompose:\n{context.task_description}",
            model=kwargs.get("model", ""),
            purpose=self.purpose,
            temperature=0.2,
        )

    def parse(self, data: dict[str, Any], context: PipelineContext) -> dict[str, Any]:
        raw = self._require(data, "subtasks", list)
        if not raw:
            raise MalformedResponseError("supervisor returned an empty subtask list")
        if len(raw) > MAX_SUBTASKS:
            raise MalformedResponseError(
                f"supervisor returned {len(raw)} subtasks, limit is {MAX_SUBTASKS}"
            )

        subtasks: list[Subtask] = []
        for position, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise MalformedResponseError(
                    f"subtask {position} is {type(item).__name__}, expected an object"
                )
            description = str(item.get("description", "")).strip()
            if not description:
                raise MalformedResponseError(f"subtask {position} has an empty description")
            # Renumber from position rather than trusting the model's `index`:
            # models skip and duplicate indices, and the pipeline keys worker
            # output by index.
            subtasks.append(
                Subtask(
                    index=position,
                    agent=str(item.get("agent") or "Worker"),
                    description=description,
                )
            )

        context.plan = subtasks
        context.plan_reasoning = str(data.get("reasoning") or "").strip() or None

        return {
            "subtasks": [
                {"index": s.index, "agent": s.agent, "description": s.description} for s in subtasks
            ],
            "reasoning": context.plan_reasoning,
        }
