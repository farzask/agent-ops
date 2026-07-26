"""Clarifier: find ambiguity in the plan and self-resolve it (TECH_SPEC 3, step 2).

No human-in-the-loop in v1 - the agent records its assumptions in the log rather
than blocking, which is the behaviour PRD 3 specifies.
"""

from __future__ import annotations

from typing import Any

from app.orchestrator.base import JSON_ONLY_INSTRUCTION, Agent, PipelineContext
from app.orchestrator.llm_client import LLMRequest

SYSTEM_PROMPT = f"""You are the Clarifier in a multi-agent pipeline. You receive an \
original task and a plan produced by a Supervisor agent. Your job is to surface \
ambiguities that would cause a Worker agent to produce the wrong thing.

There is no human available to answer questions. For every ambiguity you find, \
commit to the most reasonable assumption and state it explicitly so it is on the \
record. Do not block, and do not ask questions.

Return this exact shape:
{{
  "ambiguities": ["..."],
  "assumptions": ["..."],
  "revised_plan_notes": "guidance the Workers should apply, or a note that the \
plan is fine as written"
}}

Both lists may be empty if the task is genuinely unambiguous.

{JSON_ONLY_INSTRUCTION}"""


class ClarifierAgent(Agent):
    name = "Clarifier"
    purpose = "clarify"

    def build_prompt(self, context: PipelineContext, **kwargs: Any) -> LLMRequest:
        plan_lines = "\n".join(
            f"{s.index}. [{s.agent}] {s.description}" for s in context.plan
        )
        return LLMRequest(
            system=SYSTEM_PROMPT,
            user=(
                f"Original task:\n{context.task_description}\n\n"
                f"Supervisor's plan:\n{plan_lines}"
            ),
            model=kwargs.get("model", ""),
            purpose=self.purpose,
            temperature=0.2,
        )

    def parse(self, data: dict[str, Any], context: PipelineContext) -> dict[str, Any]:
        context.ambiguities = self._str_list(data, "ambiguities")
        context.assumptions = self._str_list(data, "assumptions")
        notes = str(data.get("revised_plan_notes") or "").strip() or None

        return {
            "ambiguities": context.ambiguities,
            "assumptions": context.assumptions,
            "revised_plan_notes": notes,
        }
