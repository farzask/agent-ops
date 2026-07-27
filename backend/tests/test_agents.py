"""Agent prompt construction and response parsing.

These are the hand-written pieces a framework would hide. Each agent's parser
must reject bad content rather than pass it downstream.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.orchestrator.base import PipelineContext, Subtask
from app.orchestrator.clarifier import ClarifierAgent
from app.orchestrator.llm_client import LLMClient, MalformedResponseError, MockProvider
from app.orchestrator.supervisor import MAX_SUBTASKS, SupervisorAgent
from app.orchestrator.verifier import VerifierAgent
from app.orchestrator.worker import WorkerAgent


@pytest.fixture
def client(settings: Settings) -> LLMClient:
    return LLMClient(MockProvider(settings), settings)


@pytest.fixture
def context() -> PipelineContext:
    return PipelineContext(task_description="Write a blog post about leak detection")


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


def test_supervisor_renumbers_subtasks_from_position(
    client: LLMClient, context: PipelineContext
) -> None:
    """Models skip and duplicate indices; the pipeline keys output by index."""
    agent = SupervisorAgent(client)
    agent.parse(
        {
            "subtasks": [
                {"index": 5, "agent": "Worker", "description": "research"},
                {"index": 5, "agent": "Worker", "description": "draft"},
                {"index": 99, "agent": "Worker", "description": "revise"},
            ]
        },
        context,
    )
    assert [s.index for s in context.plan] == [1, 2, 3]
    assert [s.description for s in context.plan] == ["research", "draft", "revise"]


def test_supervisor_defaults_missing_agent_to_worker(
    client: LLMClient, context: PipelineContext
) -> None:
    SupervisorAgent(client).parse({"subtasks": [{"description": "do the thing"}]}, context)
    assert context.plan[0].agent == "Worker"


@pytest.mark.parametrize(
    "data",
    [
        {},  # no subtasks key
        {"subtasks": []},  # empty plan
        {"subtasks": "not a list"},  # wrong type
        {"subtasks": ["a string, not an object"]},  # wrong item type
        {"subtasks": [{"description": "   "}]},  # blank description
    ],
)
def test_supervisor_rejects_bad_plans(
    client: LLMClient, context: PipelineContext, data: dict
) -> None:
    with pytest.raises(MalformedResponseError):
        SupervisorAgent(client).parse(data, context)


def test_supervisor_enforces_subtask_limit(client: LLMClient, context: PipelineContext) -> None:
    too_many = {"subtasks": [{"description": f"task {i}"} for i in range(MAX_SUBTASKS + 1)]}
    with pytest.raises(MalformedResponseError):
        SupervisorAgent(client).parse(too_many, context)


def test_supervisor_prompt_includes_the_task(client: LLMClient, context: PipelineContext) -> None:
    request = SupervisorAgent(client).build_prompt(context, model="m")
    assert context.task_description in request.user
    assert request.purpose == "plan"


# ---------------------------------------------------------------------------
# Clarifier
# ---------------------------------------------------------------------------


def test_clarifier_records_assumptions_without_blocking(
    client: LLMClient, context: PipelineContext
) -> None:
    """PRD 3: no human-in-the-loop in v1 - it self-resolves and logs."""
    payload = ClarifierAgent(client).parse(
        {
            "ambiguities": ["audience unspecified"],
            "assumptions": ["assume a technical reader"],
            "revised_plan_notes": "fine as written",
        },
        context,
    )
    assert context.assumptions == ["assume a technical reader"]
    assert context.ambiguities == ["audience unspecified"]
    assert payload["revised_plan_notes"] == "fine as written"


def test_clarifier_tolerates_empty_lists(client: LLMClient, context: PipelineContext) -> None:
    ClarifierAgent(client).parse({}, context)
    assert context.assumptions == []
    assert context.ambiguities == []


def test_clarifier_prompt_includes_the_plan(client: LLMClient, context: PipelineContext) -> None:
    context.plan = [Subtask(1, "Worker", "research the topic")]
    request = ClarifierAgent(client).build_prompt(context, model="m")
    assert "research the topic" in request.user


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def test_worker_stores_output_under_its_subtask_index(
    client: LLMClient, context: PipelineContext
) -> None:
    subtask = Subtask(2, "Worker", "draft the post")
    agent = WorkerAgent(client, subtask)
    assert agent.name == "Worker-2"

    agent.parse({"output": "the draft", "notes": "ok"}, context)
    assert context.subtask_outputs == {2: "the draft"}


@pytest.mark.parametrize("data", [{}, {"output": ""}, {"output": "   "}, {"output": 42}])
def test_worker_rejects_empty_or_non_string_output(
    client: LLMClient, context: PipelineContext, data: dict
) -> None:
    agent = WorkerAgent(client, Subtask(1, "Worker", "draft"))
    with pytest.raises(MalformedResponseError):
        agent.parse(data, context)


def test_worker_prompt_carries_assumptions_and_prior_output(
    client: LLMClient, context: PipelineContext
) -> None:
    context.assumptions = ["assume 500 words"]
    context.subtask_outputs = {1: "research notes here"}
    agent = WorkerAgent(client, Subtask(2, "Worker", "draft the post"))

    request = agent.build_prompt(context, model="m")

    assert "assume 500 words" in request.user
    assert "research notes here" in request.user
    assert "draft the post" in request.user


def test_worker_prompt_omits_later_subtask_output(
    client: LLMClient, context: PipelineContext
) -> None:
    """Subtask 1 must not be shown output from subtask 3 during a rework."""
    context.subtask_outputs = {1: "first", 3: "third"}
    agent = WorkerAgent(client, Subtask(1, "Worker", "redo the first"))

    request = agent.build_prompt(context, model="m")

    assert "third" not in request.user


def test_worker_prompt_includes_verifier_feedback_on_rework(
    client: LLMClient, context: PipelineContext
) -> None:
    context.verifier_feedback = "missing the cost analysis"
    agent = WorkerAgent(client, Subtask(1, "Worker", "draft"))
    assert "missing the cost analysis" in agent.build_prompt(context, model="m").user


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


def test_verifier_approval_clears_feedback(client: LLMClient, context: PipelineContext) -> None:
    """Stale approval feedback in a later Worker prompt is confusing noise."""
    context.verifier_feedback = "an older rejection"
    payload = VerifierAgent(client).parse(
        {"approved": True, "score": 0.9, "feedback": "looks good"}, context
    )
    assert payload["approved"] is True
    assert context.verifier_feedback is None
    assert context.verifier_score == pytest.approx(0.9)


def test_verifier_rejection_carries_feedback_forward(
    client: LLMClient, context: PipelineContext
) -> None:
    context.plan = [Subtask(1, "Worker", "draft")]
    payload = VerifierAgent(client).parse(
        {
            "approved": False,
            "feedback": "does not answer the question",
            "reject_subtask_index": 1,
        },
        context,
    )
    assert payload["approved"] is False
    assert payload["reject_subtask_index"] == 1
    assert context.verifier_feedback == "does not answer the question"


def test_verifier_rejection_without_feedback_is_malformed(
    client: LLMClient, context: PipelineContext
) -> None:
    """A Worker cannot act on an empty rejection."""
    with pytest.raises(MalformedResponseError):
        VerifierAgent(client).parse({"approved": False, "feedback": ""}, context)


def test_verifier_coerces_stringly_typed_booleans(
    client: LLMClient, context: PipelineContext
) -> None:
    assert VerifierAgent(client).parse({"approved": "true"}, context)["approved"] is True


@pytest.mark.parametrize("approved", [1, 0, "yes", "maybe", None, [], {}])
def test_verifier_rejects_ambiguous_approval(
    client: LLMClient, context: PipelineContext, approved: object
) -> None:
    """Mis-reading an approval would ship unverified output - never guess."""
    with pytest.raises(MalformedResponseError):
        VerifierAgent(client).parse({"approved": approved, "feedback": "f"}, context)


def test_verifier_missing_approved_key_is_malformed(
    client: LLMClient, context: PipelineContext
) -> None:
    with pytest.raises(MalformedResponseError):
        VerifierAgent(client).parse({"score": 0.5, "feedback": "f"}, context)


def test_verifier_clamps_score_and_drops_invalid_reject_index(
    client: LLMClient, context: PipelineContext
) -> None:
    context.plan = [Subtask(1, "Worker", "draft")]
    payload = VerifierAgent(client).parse(
        {
            "approved": False,
            "feedback": "redo it",
            "score": 4.7,
            "reject_subtask_index": 42,  # not in the plan
        },
        context,
    )
    assert payload["score"] == pytest.approx(1.0)
    assert payload["reject_subtask_index"] is None


def test_verifier_prompt_includes_combined_output(
    client: LLMClient, context: PipelineContext
) -> None:
    context.plan = [Subtask(1, "Worker", "a"), Subtask(2, "Worker", "b")]
    context.subtask_outputs = {1: "part one", 2: "part two"}

    request = VerifierAgent(client).build_prompt(context, model="m")

    assert "part one" in request.user and "part two" in request.user


def test_combined_output_is_ordered_by_subtask_index() -> None:
    context = PipelineContext(task_description="t")
    # Inserted out of order on purpose.
    context.subtask_outputs = {3: "third", 1: "first", 2: "second"}
    assert context.combined_output() == "first\n\nsecond\n\nthird"
