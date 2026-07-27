"""Hand-written orchestration. This is the module the project exists to show.

TECH_SPEC 3: Supervisor -> Clarifier -> Worker(s) -> Verifier -> Done, with the
state machine from 3.1 and the retry/rework policy from 3.2.

Everything here is explicit. There is no framework deciding what runs next, no
hidden agent loop, and no implicit shared memory - the plan flows through
:class:`PipelineContext`, and every transition goes through :class:`EventEmitter`
so the frontend sees exactly what happened.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.db_models import (
    AgentRun,
    AgentStatus,
    Job,
    JobStatus,
    LogLevel,
)
from app.orchestrator.base import Agent, PipelineContext
from app.orchestrator.clarifier import ClarifierAgent
from app.orchestrator.events import EventEmitter, EventPublisher
from app.orchestrator.llm_client import (
    LLMClient,
    LLMError,
    PermanentLLMError,
    RetriesExhaustedError,
)
from app.orchestrator.supervisor import SupervisorAgent
from app.orchestrator.verifier import VerifierAgent
from app.orchestrator.worker import WorkerAgent

logger = logging.getLogger(__name__)

# Fixed v1 topology. The frontend renders placeholder nodes from this list before
# any worker exists, so the diagram is never empty on first paint.
STATIC_NODES = ("Supervisor", "Clarifier", "Verifier", "Done")


class PipelineFailure(Exception):
    """A pipeline halted. Message is persisted as ``jobs.failure_reason``."""


@dataclass(slots=True)
class PipelineResult:
    job_id: uuid.UUID
    status: JobStatus
    final_output: str | None
    failure_reason: str | None


class Pipeline:
    """Runs one job to completion or documented failure."""

    def __init__(
        self,
        session: AsyncSession,
        publisher: EventPublisher,
        llm_client: LLMClient,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._publisher = publisher
        self._llm = llm_client
        self._settings = settings or get_settings()

    # -----------------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------------

    async def run(self, job_id: uuid.UUID) -> PipelineResult:
        job = await self._session.get(Job, job_id)
        if job is None:
            raise PipelineFailure(f"job {job_id} not found")

        emitter = EventEmitter(self._session, self._publisher, job_id)
        context = PipelineContext(task_description=job.task_description)

        if job.status is not JobStatus.QUEUED:
            # Guard against double-consumption of a queue message.
            raise PipelineFailure(f"job {job_id} is {job.status.value}, expected queued")

        await emitter.job_status(job, JobStatus.RUNNING)
        await emitter.log(f"Pipeline started. Task: {job.task_description}", level=LogLevel.INFO)

        try:
            await self._run_stages(job, emitter, context)
        except PipelineFailure as exc:
            reason = str(exc)
            await emitter.log(f"Pipeline halted: {reason}", level=LogLevel.ERROR)
            await emitter.job_status(job, JobStatus.FAILED, failure_reason=reason)
            return PipelineResult(job_id, JobStatus.FAILED, None, reason)
        except Exception as exc:  # unexpected - still must not leave job running
            reason = f"unexpected {type(exc).__name__}: {exc}"
            logger.exception("unhandled error in pipeline for job %s", job_id)
            await emitter.log(f"Pipeline crashed: {reason}", level=LogLevel.ERROR)
            await emitter.job_status(job, JobStatus.FAILED, failure_reason=reason)
            return PipelineResult(job_id, JobStatus.FAILED, None, reason)

        final_output = context.combined_output()
        await emitter.log("Pipeline completed successfully.", level=LogLevel.INFO)
        await emitter.job_status(job, JobStatus.COMPLETED, final_output=final_output)
        return PipelineResult(job_id, JobStatus.COMPLETED, final_output, None)

    # -----------------------------------------------------------------------
    # Stages
    # -----------------------------------------------------------------------

    async def _run_stages(self, job: Job, emitter: EventEmitter, context: PipelineContext) -> None:
        # --- 1. Supervisor: decompose -------------------------------------
        supervisor = SupervisorAgent(self._llm)
        sup_run = await self._ensure_run(job.id, supervisor.name, 0)
        await self._execute(supervisor, sup_run, emitter, context)
        await emitter.log(
            f"Decomposed task into {len(context.plan)} subtask(s).",
            agent_name=supervisor.name,
            agent_run_id=sup_run.id,
        )
        for subtask in context.plan:
            await emitter.log(
                f"Subtask {subtask.index}: {subtask.description}",
                agent_name=supervisor.name,
                agent_run_id=sup_run.id,
            )

        # --- 2. Clarifier: resolve ambiguity ------------------------------
        clarifier = ClarifierAgent(self._llm)
        clar_run = await self._ensure_run(job.id, clarifier.name, 1)
        await self._execute(clarifier, clar_run, emitter, context)
        if context.ambiguities:
            for item in context.ambiguities:
                await emitter.log(
                    f"Ambiguity identified: {item}",
                    level=LogLevel.WARN,
                    agent_name=clarifier.name,
                    agent_run_id=clar_run.id,
                )
        for item in context.assumptions:
            await emitter.log(
                f"Assumption recorded (no human-in-the-loop in v1): {item}",
                agent_name=clarifier.name,
                agent_run_id=clar_run.id,
            )
        if not context.ambiguities and not context.assumptions:
            await emitter.log(
                "No ambiguities found; plan is executable as written.",
                agent_name=clarifier.name,
                agent_run_id=clar_run.id,
            )

        # --- 3 & 4. Workers, then Verifier, with bounded rework -----------
        # Worker sequence indices start after the two fixed leading agents.
        worker_base_index = 2
        rework_cycles = 0

        while True:
            await self._run_workers(job, emitter, context, worker_base_index)

            verifier = VerifierAgent(self._llm)
            verifier_index = worker_base_index + len(context.plan)
            ver_run = await self._ensure_run(job.id, verifier.name, verifier_index)
            payload = await self._execute(verifier, ver_run, emitter, context)

            if payload.get("approved"):
                score = payload.get("score")
                score_text = f" (score {score:.2f})" if isinstance(score, float) else ""
                await emitter.log(
                    f"Verifier approved the output{score_text}.",
                    agent_name=verifier.name,
                    agent_run_id=ver_run.id,
                )
                return

            # Rejected. This is NOT a technical failure - it has its own budget.
            rework_cycles += 1
            feedback = payload.get("feedback") or "no feedback provided"

            if rework_cycles > self._settings.max_rework_cycles:
                raise PipelineFailure(
                    f"Verifier rejected the output after "
                    f"{self._settings.max_rework_cycles} rework cycle(s). "
                    f"Last feedback: {feedback}"
                )

            ver_run.rework_count = rework_cycles
            await self._session.commit()

            reject_index = payload.get("reject_subtask_index")
            target = f"subtask {reject_index}" if reject_index is not None else "all subtasks"
            await emitter.log(
                f"Verifier rejected the output (rework cycle {rework_cycles} of "
                f"{self._settings.max_rework_cycles}), routing {target} back to "
                f"the Worker. Feedback: {feedback}",
                level=LogLevel.WARN,
                agent_name=verifier.name,
                agent_run_id=ver_run.id,
            )

            # Clear the outputs that need redoing so the Worker loop re-runs them.
            if reject_index is not None:
                context.subtask_outputs.pop(reject_index, None)
            else:
                context.subtask_outputs.clear()

    async def _run_workers(
        self,
        job: Job,
        emitter: EventEmitter,
        context: PipelineContext,
        base_index: int,
    ) -> None:
        """Execute every subtask that does not yet have output, in order.

        Sequential by design (TECH_SPEC 3, step 3) - subtask N may depend on
        1..N-1, and parallelism is explicitly a v2 item.
        """
        for subtask in context.plan:
            if subtask.index in context.subtask_outputs:
                continue  # survived a rework cycle, no need to redo it

            agent = WorkerAgent(self._llm, subtask)
            sequence_index = base_index + subtask.index - 1
            run = await self._ensure_run(job.id, agent.name, sequence_index)
            await self._execute(agent, run, emitter, context)
            await emitter.log(
                f"Completed subtask {subtask.index}: {subtask.description}",
                agent_name=agent.name,
                agent_run_id=run.id,
            )

    # -----------------------------------------------------------------------
    # Single agent execution: the retry loop
    # -----------------------------------------------------------------------

    async def _execute(
        self,
        agent: Agent,
        run: AgentRun,
        emitter: EventEmitter,
        context: PipelineContext,
    ) -> dict:
        """Run one agent through queued -> running -> completed | failed.

        Technical retries are handled inside :class:`LLMClient`; this method
        translates them into observable state via the ``on_retry`` hook, so a
        retry is visualized rather than hidden (PRD 7.6).
        """
        await emitter.agent_status(run, AgentStatus.QUEUED)

        run.input_payload = {
            "agent": agent.name,
            "purpose": agent.purpose,
            "task_description": context.task_description,
            "subtask": getattr(getattr(agent, "subtask", None), "description", None),
            "assumptions": context.assumptions,
            "verifier_feedback": context.verifier_feedback,
        }
        await self._session.commit()

        await emitter.agent_status(run, AgentStatus.RUNNING)
        await emitter.log(
            f"{agent.name} started (attempt {run.attempt_count}).",
            agent_name=agent.name,
            agent_run_id=run.id,
        )

        async def on_retry(attempt: int, delay: float, error: BaseException) -> None:
            # Surface the retry as real state, not just a log line: the diagram
            # node must visibly enter `retrying`.
            await emitter.log(
                f"{agent.name} attempt {attempt} failed "
                f"({type(error).__name__}: {error}). Retrying in {delay:.1f}s.",
                level=LogLevel.WARN,
                agent_name=agent.name,
                agent_run_id=run.id,
            )
            await emitter.agent_status(run, AgentStatus.FAILED, failure_reason=str(error))
            await emitter.agent_status(run, AgentStatus.RETRYING)
            await emitter.agent_status(run, AgentStatus.RUNNING)

        try:
            payload = await agent.run(context, on_retry=on_retry, model=self._settings.llm_model)
        except PermanentLLMError as exc:
            reason = f"permanent LLM failure: {exc}"
            await emitter.log(
                f"{agent.name} failed permanently, no retry attempted: {exc}",
                level=LogLevel.ERROR,
                agent_name=agent.name,
                agent_run_id=run.id,
            )
            await emitter.agent_status(run, AgentStatus.FAILED, failure_reason=reason)
            raise PipelineFailure(f"{agent.name}: {reason}") from exc
        except RetriesExhaustedError as exc:
            reason = (
                f"retries exhausted after {exc.attempts} attempt(s): "
                f"{type(exc.last_error).__name__}: {exc.last_error}"
            )
            await emitter.log(
                f"{agent.name} exhausted its retry budget: {reason}",
                level=LogLevel.ERROR,
                agent_name=agent.name,
                agent_run_id=run.id,
            )
            await emitter.agent_status(run, AgentStatus.FAILED, failure_reason=reason)
            raise PipelineFailure(f"{agent.name}: {reason}") from exc
        except LLMError as exc:
            reason = f"{type(exc).__name__}: {exc}"
            await emitter.agent_status(run, AgentStatus.FAILED, failure_reason=reason)
            raise PipelineFailure(f"{agent.name}: {reason}") from exc

        await emitter.agent_status(run, AgentStatus.COMPLETED, output_payload=payload)
        return payload

    # -----------------------------------------------------------------------
    # Agent run rows
    # -----------------------------------------------------------------------

    async def _ensure_run(
        self, job_id: uuid.UUID, agent_name: str, sequence_index: int
    ) -> AgentRun:
        """Fetch or create the ``agent_runs`` row for this pipeline position.

        Reused across rework cycles so the UI shows one node per agent with a
        rising attempt count, rather than a growing pile of duplicate nodes.
        """
        result = await self._session.execute(
            select(AgentRun).where(
                AgentRun.job_id == job_id,
                AgentRun.agent_name == agent_name,
                AgentRun.sequence_index == sequence_index,
            )
        )
        run = result.scalar_one_or_none()
        if run is not None:
            return run

        run = AgentRun(
            job_id=job_id,
            agent_name=agent_name,
            sequence_index=sequence_index,
            status=AgentStatus.IDLE,
            attempt_count=0,
            rework_count=0,
            input_payload={},
        )
        self._session.add(run)
        await self._session.commit()
        return run
