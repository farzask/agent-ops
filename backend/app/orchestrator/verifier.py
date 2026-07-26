"""Verifier: approve the combined output or send it back for rework.

TECH_SPEC 3, step 4. A rejection routes back to the responsible Worker and is
counted as a *rework cycle*, not a technical failure - it must never consume the
technical retry budget (TECH_SPEC 3.2).
"""

from __future__ import annotations

from typing import Any

from app.orchestrator.base import JSON_ONLY_INSTRUCTION, Agent, PipelineContext
from app.orchestrator.llm_client import LLMRequest, MalformedResponseError

SYSTEM_PROMPT = f"""You are the Verifier in a multi-agent pipeline. You receive an \
original task and the combined output of the Worker agents. Decide whether the \
output actually satisfies the original task.

Approve if the output is responsive, internally consistent, and complete enough \
to hand back to the requester. Reject only for a substantive problem - a missing \
requirement, a factual contradiction, or an off-topic response. Do not reject \
over style preferences or minor wording.

If you reject, your feedback must be specific and actionable, because it is fed \
straight back to the Worker that produced the output.

Return this exact shape:
{{
  "approved": true,
  "score": 0.0,
  "feedback": "why you approved, or exactly what must change",
  "reject_subtask_index": null
}}

"score" is your confidence in the output from 0.0 to 1.0. Set \
"reject_subtask_index" to the subtask number that needs rework when rejecting, \
or null when approving.

{JSON_ONLY_INSTRUCTION}"""


class VerifierAgent(Agent):
    name = "Verifier"
    purpose = "verify"

    def build_prompt(self, context: PipelineContext, **kwargs: Any) -> LLMRequest:
        plan_lines = "\n".join(
            f"{s.index}. {s.description}" for s in context.plan
        )
        sections = [
            f"Original task:\n{context.task_description}",
            f"Plan that was executed:\n{plan_lines}",
        ]
        if context.assumptions:
            sections.append(
                "Assumptions the pipeline committed to:\n"
                + "\n".join(f"- {a}" for a in context.assumptions)
            )
        sections.append(f"Combined worker output:\n{context.combined_output()}")

        return LLMRequest(
            system=SYSTEM_PROMPT,
            user="\n\n---\n\n".join(sections),
            model=kwargs.get("model", ""),
            purpose=self.purpose,
            temperature=0.1,
        )

    def parse(self, data: dict[str, Any], context: PipelineContext) -> dict[str, Any]:
        if "approved" not in data:
            raise MalformedResponseError(
                f"verifier response missing 'approved' (got keys: {sorted(data)})"
            )

        approved = data["approved"]
        if not isinstance(approved, bool):
            # Models sometimes emit the string "true". Accept the obvious
            # coercions and reject anything genuinely ambiguous rather than
            # guessing - a mis-read approval would ship unverified output.
            if isinstance(approved, str) and approved.strip().lower() in {
                "true",
                "false",
            }:
                approved = approved.strip().lower() == "true"
            else:
                raise MalformedResponseError(
                    f"verifier 'approved' should be a boolean, got {approved!r}"
                )

        feedback = str(data.get("feedback") or "").strip() or None
        if not approved and not feedback:
            raise MalformedResponseError(
                "verifier rejected the output without providing feedback; the "
                "Worker cannot act on an empty rejection"
            )

        score: float | None = None
        raw_score = data.get("score")
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            score = max(0.0, min(1.0, float(raw_score)))

        reject_index: int | None = None
        raw_index = data.get("reject_subtask_index")
        if isinstance(raw_index, int) and not isinstance(raw_index, bool):
            valid = {s.index for s in context.plan}
            if raw_index in valid:
                reject_index = raw_index

        context.verifier_score = score
        # Only carry feedback forward on rejection - stale approval feedback in
        # a later Worker prompt is confusing noise.
        context.verifier_feedback = None if approved else feedback

        return {
            "approved": approved,
            "score": score,
            "feedback": feedback,
            "reject_subtask_index": reject_index,
        }
