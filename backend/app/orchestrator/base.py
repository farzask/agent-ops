"""Shared agent scaffolding.

Deliberately thin. This is *not* a framework abstraction - it is the small
amount of shared structure four hand-written agents genuinely have in common
(a name, a prompt pair, and a parse step). Control flow lives in
``pipeline.py``, where it is readable in one place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.orchestrator.llm_client import (
    LLMClient,
    LLMRequest,
    MalformedResponseError,
    RetryHook,
)


@dataclass(slots=True)
class Subtask:
    index: int
    agent: str
    description: str


@dataclass(slots=True)
class PipelineContext:
    """State threaded through the pipeline.

    Written by each agent, read by the next. This explicit hand-off is what a
    framework would hide behind shared memory or a message bus.
    """

    task_description: str
    plan: list[Subtask] = field(default_factory=list)
    plan_reasoning: str | None = None
    assumptions: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    # Worker output keyed by subtask index.
    subtask_outputs: dict[int, str] = field(default_factory=dict)
    verifier_feedback: str | None = None
    verifier_score: float | None = None

    def combined_output(self) -> str:
        return "\n\n".join(
            self.subtask_outputs[i] for i in sorted(self.subtask_outputs)
        )


class Agent(ABC):
    """One LLM-backed step."""

    #: Display name; also the label shown on the pipeline diagram node.
    name: str = "Agent"
    #: Tag passed to the provider, used for logging and mock response shaping.
    purpose: str = "generic"

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    @abstractmethod
    def build_prompt(self, context: PipelineContext, **kwargs: Any) -> LLMRequest: ...

    @abstractmethod
    def parse(self, data: dict[str, Any], context: PipelineContext) -> dict[str, Any]:
        """Validate and apply the parsed response to ``context``.

        Returns the structured payload persisted to ``agent_runs.output_payload``.
        Raise :class:`MalformedResponseError` if the response is the right shape
        of JSON but the wrong *content* - that consumes a retry, which is what
        should happen.
        """

    async def run(
        self,
        context: PipelineContext,
        *,
        on_retry: RetryHook | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request = self.build_prompt(context, **kwargs)
        data, _response = await self._client.complete_json(request, on_retry=on_retry)
        return self.parse(data, context)

    # -- helpers for subclasses -------------------------------------------

    @staticmethod
    def _require(data: dict[str, Any], key: str, expected: type) -> Any:
        if key not in data:
            raise MalformedResponseError(
                f"response missing required key {key!r} (got keys: {sorted(data)})"
            )
        value = data[key]
        if not isinstance(value, expected):
            raise MalformedResponseError(
                f"key {key!r} should be {expected.__name__}, got "
                f"{type(value).__name__}"
            )
        return value

    @staticmethod
    def _str_list(data: dict[str, Any], key: str) -> list[str]:
        raw = data.get(key) or []
        if not isinstance(raw, list):
            raise MalformedResponseError(f"key {key!r} should be a list")
        return [str(item) for item in raw]


#: Appended to every system prompt. Hand-written JSON discipline - this is what
#: a framework's "structured output" feature does under the hood.
JSON_ONLY_INSTRUCTION = (
    "Respond with a single JSON object and nothing else. No prose before or "
    "after it, no markdown code fences. If you cannot complete the task, still "
    "return valid JSON in the requested shape and explain the problem in the "
    "relevant text field."
)
