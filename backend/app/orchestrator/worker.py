"""Worker: execute one subtask from the plan (TECH_SPEC 3, step 3).

v1 ships a single generic Worker type that executes subtasks sequentially.
Parallelising independent subtasks is a v2 enhancement per the spec - do not add
it here without updating TECH_SPEC 3.
"""

from __future__ import annotations

from typing import Any

from app.orchestrator.base import (
    JSON_ONLY_INSTRUCTION,
    Agent,
    PipelineContext,
    Subtask,
)
from app.orchestrator.llm_client import LLMRequest, MalformedResponseError

SYSTEM_PROMPT = f"""You are a Worker in a multi-agent pipeline. You execute exactly \
one subtask and produce its deliverable. You do not plan, and you do not review \
your own work - other agents handle that.

You are given the original task for context, the assumptions the Clarifier \
committed to, the outputs of previous subtasks, and the single subtask assigned \
to you. Honour the assumptions. Build on the previous outputs rather than \
repeating them.

If the Verifier previously rejected this work, its feedback is included. Address \
that feedback directly.

Return this exact shape:
{{
  "output": "the deliverable for this subtask",
  "notes": "anything the next Worker or the Verifier should know"
}}

{JSON_ONLY_INSTRUCTION}"""


class WorkerAgent(Agent):
    purpose = "work"

    def __init__(self, client, subtask: Subtask) -> None:
        super().__init__(client)
        self.subtask = subtask
        # Node label on the pipeline diagram, e.g. "Worker-1".
        self.name = f"Worker-{subtask.index}"

    def build_prompt(self, context: PipelineContext, **kwargs: Any) -> LLMRequest:
        sections = [
            f"Original task:\n{context.task_description}",
        ]

        if context.assumptions:
            sections.append(
                "Assumptions committed to by the Clarifier:\n"
                + "\n".join(f"- {a}" for a in context.assumptions)
            )

        previous = [
            f"Subtask {i} output:\n{context.subtask_outputs[i]}"
            for i in sorted(context.subtask_outputs)
            if i < self.subtask.index
        ]
        if previous:
            sections.append("Previous subtask outputs:\n\n" + "\n\n".join(previous))

        if context.verifier_feedback:
            sections.append(
                "The Verifier rejected the previous attempt with this feedback:\n"
                f"{context.verifier_feedback}"
            )

        sections.append(
            f"Your assigned subtask ({self.subtask.index}):\n"
            f"{self.subtask.description}"
        )

        return LLMRequest(
            system=SYSTEM_PROMPT,
            user="\n\n---\n\n".join(sections),
            model=kwargs.get("model", ""),
            purpose=self.purpose,
            temperature=0.4,
        )

    def parse(self, data: dict[str, Any], context: PipelineContext) -> dict[str, Any]:
        output = str(self._require(data, "output", str)).strip()
        if not output:
            raise MalformedResponseError(
                f"worker for subtask {self.subtask.index} returned an empty output"
            )

        context.subtask_outputs[self.subtask.index] = output
        notes = str(data.get("notes") or "").strip() or None

        return {
            "subtask_index": self.subtask.index,
            "subtask_description": self.subtask.description,
            "output": output,
            "notes": notes,
        }
