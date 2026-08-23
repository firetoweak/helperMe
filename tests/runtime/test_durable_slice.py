from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helperme.runtime import (
    AgentRuntime,
    AttemptPhase,
    CancelTool,
    Command,
    CommandAuthorized,
    CommandOutcome,
    CommandOutcomeReceived,
    CommandPhase,
    CommandRecoveryRequired,
    CommandReconcileStarted,
    CommandRejected,
    DomainFactCommitted,
    DeliveryConflictError,
    DeliveryIdentity,
    DispatchAttemptConfirmedNoEffect,
    DispatchAttemptStarted,
    EventDraft,
    ExternalOperationAccepted,
    InvokeTool,
    LeaseLostError,
    MemoryJournal,
    ModelDecision,
    OutcomeStatus,
    RecoveryContract,
    RecoveryIndeterminate,
    RecoveryNoEffect,
    RetrySemantics,
    RunningRecovery,
    RuntimeCompleted,
    RuntimeTerminated,
    SqliteJournal,
    StateProjector,
    Step,
    StepClaimRequest,
    StepCommitted,
    TerminationRequested,
    ToolBinding,
    ToolTerminal,
    UserInterruptReceived,
    UserMessageReceived,
)
from helperme.runtime.codec import (
    EVENT_SCHEMA_VERSION,
    decode_payload,
    delivery_fingerprint,
    encode_payload,
)


NOW = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)


class ManualClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class NamespacedIds:
    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{self.namespace}-{prefix}-{self.value}"


class ModelMustNotRun:
    async def decide(self, _frame):
        raise AssertionError("model must not run")


class BlockingAcquireSqliteJournal(SqliteJournal):
    def __init__(self, path, *, clock) -> None:
        self.acquire_started = threading.Event()
        self.release_acquire = threading.Event()
        super().__init__(path, clock=clock)

    def _acquire_step_tx(self, *args):
        self.acquire_started.set()
        if not self.release_acquire.wait(timeout=2):
            raise TimeoutError("test did not release acquire")
        return super()._acquire_step_tx(*args)


class BlockingSnapshotSqliteJournal(SqliteJournal):
    def __init__(self, path, *, clock) -> None:
        self.snapshot_started = threading.Event()
        self.release_snapshot = threading.Event()
        super().__init__(path, clock=clock)

    def _snapshot_sync(self, stream_id):
        self.snapshot_started.set()
        if not self.release_snapshot.wait(timeout=2):
            raise TimeoutError("test did not release snapshot")
        return super()._snapshot_sync(stream_id)


class SqliteDurableSliceTest(unittest.IsolatedAsyncioTestCase):
    STREAM_ID = "durable-stream"

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self._temporary_directory.name) / "runtime.db"
        )
        self.clock = ManualClock()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def journal(self) -> SqliteJournal:
        return SqliteJournal(self.database_path, clock=self.clock)

    async def test_empty_stream_identity_is_idempotent_and_durable(self):
        journal = self.journal()

        self.assertTrue(await journal.create_stream(self.STREAM_ID))
        self.assertFalse(await journal.create_stream(self.STREAM_ID))
        self.assertTrue(await journal.stream_exists(self.STREAM_ID))
        self.assertFalse(await journal.stream_exists("missing-stream"))
        self.assertEqual(await journal.snapshot(self.STREAM_ID), ())

        reopened = self.journal()
        self.assertTrue(await reopened.stream_exists(self.STREAM_ID))
        self.assertEqual(await reopened.snapshot(self.STREAM_ID), ())

    async def test_delivery_deduplicates_across_workers_and_restart(self):
        journal_a = self.journal()
        journal_b = self.journal()
        delivery = DeliveryIdentity("user", "delivery-1")
        draft_a = EventDraft(
            event_id="message-a",
            stream_id=self.STREAM_ID,
            payload=UserMessageReceived("hello"),
            occurred_at=NOW,
            delivery=delivery,
        )
        draft_b = EventDraft(
            event_id="message-b",
            stream_id=self.STREAM_ID,
            payload=UserMessageReceived("hello"),
            occurred_at=NOW + timedelta(seconds=1),
            delivery=delivery,
        )

        left, right = await asyncio.gather(
            journal_a.accept_delivery(draft_a),
            journal_b.accept_delivery(draft_b),
        )

        self.assertEqual({left.inserted, right.inserted}, {True, False})
        self.assertEqual(left.event, right.event)
        self.assertEqual(len(await journal_a.snapshot(self.STREAM_ID)), 1)

        with self.assertRaises(DeliveryConflictError):
            await journal_b.accept_delivery(EventDraft(
                event_id="message-conflict",
                stream_id=self.STREAM_ID,
                payload=UserMessageReceived("different"),
                occurred_at=NOW,
                delivery=delivery,
            ))

        reopened = self.journal()
        replayed = await reopened.snapshot(self.STREAM_ID)
        self.assertEqual(replayed, (left.event,))
        duplicate = await reopened.accept_delivery(draft_b)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.event, left.event)

    async def test_step_lease_fences_stale_worker_and_commit_is_idempotent(self):
        journal_a = self.journal()
        journal_b = self.journal()
        await self._accept_message(journal_a)
        frame = StateProjector().project(
            self.STREAM_ID,
            await journal_a.snapshot(self.STREAM_ID),
        ).next_decision
        self.assertIsNotNone(frame)
        request = StepClaimRequest(
            stream_id=self.STREAM_ID,
            trigger_event_id=frame.trigger_event.event_id,
            decision_cursor=frame.decision_cursor,
            basis_state_version=frame.basis_state_version,
            observed_journal_position=frame.observed_journal_position,
        )

        lease_a, lease_b = await asyncio.gather(
            journal_a.acquire_step(
                request,
                token="claim-a",
                owner_id="worker-a",
                lease_seconds=10,
            ),
            journal_b.acquire_step(
                request,
                token="claim-b",
                owner_id="worker-b",
                lease_seconds=10,
            ),
        )
        self.assertEqual(
            sum(lease is not None for lease in (lease_a, lease_b)),
            1,
        )
        old_lease = lease_a or lease_b
        self.clock.advance(11)
        new_lease = await journal_b.acquire_step(
            request,
            token="claim-c",
            owner_id="worker-c",
            lease_seconds=10,
        )
        self.assertIsNotNone(new_lease)
        draft = self._step_draft(
            frame,
            event_id="step-event",
            step_id="step-1",
        )

        with self.assertRaises(LeaseLostError):
            await journal_a.commit_step(old_lease, draft)
        committed = await journal_b.commit_step(new_lease, draft)
        retried = await journal_b.commit_step(new_lease, draft)

        self.assertEqual(retried, committed)
        step_events = [
            event
            for event in await journal_a.snapshot(self.STREAM_ID)
            if isinstance(event.payload, StepCommitted)
        ]
        self.assertEqual(step_events, [committed])

    async def test_step_claim_token_is_globally_unique_in_both_journals(self):
        for name, journal in (
            ("memory", MemoryJournal(clock=self.clock)),
            ("sqlite", self.journal()),
        ):
            with self.subTest(journal=name):
                requests = []
                for suffix in ("a", "b"):
                    stream_id = f"claim-stream-{suffix}"
                    await journal.accept_delivery(EventDraft(
                        event_id=f"claim-message-{suffix}",
                        stream_id=stream_id,
                        payload=UserMessageReceived("claim"),
                        occurred_at=NOW,
                        delivery=DeliveryIdentity(
                            "user",
                            f"claim-delivery-{suffix}",
                        ),
                    ))
                    frame = StateProjector().project(
                        stream_id,
                        await journal.snapshot(stream_id),
                    ).next_decision
                    requests.append(StepClaimRequest(
                        stream_id=stream_id,
                        trigger_event_id=frame.trigger_event.event_id,
                        decision_cursor=frame.decision_cursor,
                        basis_state_version=frame.basis_state_version,
                        observed_journal_position=(
                            frame.observed_journal_position
                        ),
                    ))

                lease = await journal.acquire_step(
                    requests[0],
                    token="global-claim-token",
                    owner_id="worker-a",
                    lease_seconds=10,
                )
                self.assertIsNotNone(lease)
                with self.assertRaises((ValueError, sqlite3.IntegrityError)):
                    await journal.acquire_step(
                        requests[1],
                        token="global-claim-token",
                        owner_id="worker-b",
                        lease_seconds=10,
                    )

    async def test_pending_command_has_one_attempt_and_one_invocation(self):
        seed = self.journal()
        command, _ = await self._seed_pending(seed)
        calls: list[str] = []

        async def handler(context, _arguments):
            calls.append(context.attempt_id)
            return "done"

        runtime_a = self._runtime(
            self.journal(),
            ToolBinding(handler),
            namespace="a",
        )
        runtime_b = self._runtime(
            self.journal(),
            ToolBinding(handler),
            namespace="b",
        )

        started_a, started_b = await asyncio.gather(
            runtime_a.dispatcher.start_pending(self.STREAM_ID),
            runtime_b.dispatcher.start_pending(self.STREAM_ID),
        )
        await asyncio.gather(
            runtime_a.dispatcher.wait_all(),
            runtime_b.dispatcher.wait_all(),
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            sorted((started_a, started_b), key=len),
            [(), (command.command_id,)],
        )
        events = await self.journal().snapshot(self.STREAM_ID)
        self.assertEqual(
            sum(isinstance(event.payload, DispatchAttemptStarted)
                for event in events),
            1,
        )
        self.assertEqual(
            sum(isinstance(event.payload, CommandOutcomeReceived)
                for event in events),
            1,
        )
        state = StateProjector().project(self.STREAM_ID, events).state
        self.assertIs(
            state.command(command.command_id).phase,
            CommandPhase.TERMINAL,
        )

    async def test_attempt_claim_is_atomic_across_store_instances(self):
        seed = self.journal()
        command, step_event = await self._seed_pending(seed)
        journal_a = self.journal()
        journal_b = self.journal()
        draft_a = self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-a",
        )
        draft_b = self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-b",
        )

        left, right = await asyncio.gather(
            journal_a.start_attempt(draft_a),
            journal_b.start_attempt(draft_b),
        )

        self.assertEqual(sum(event is not None for event in (left, right)), 1)
        events = await seed.snapshot(self.STREAM_ID)
        self.assertEqual(
            sum(isinstance(event.payload, DispatchAttemptStarted)
                for event in events),
            1,
        )

    async def test_unknown_requires_recovery_decision_without_blind_retry(self):
        journal = self.journal()
        command, step_event = await self._seed_pending(journal)
        dispatch = await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-1",
        ))
        self.assertIsNotNone(dispatch)
        calls = 0

        async def handler(_context, _arguments):
            nonlocal calls
            calls += 1
            raise AssertionError("unknown command must not be invoked")

        runtime = self._runtime(
            self.journal(),
            ToolBinding(handler),
            namespace="recovery",
        )
        await runtime.recover_once(self.STREAM_ID)
        active_events = await self.journal().snapshot(self.STREAM_ID)
        self.assertFalse(any(
            isinstance(event.payload, CommandRecoveryRequired)
            for event in active_events
        ))

        self.clock.advance(31)
        await runtime.recover_once(self.STREAM_ID)
        await runtime.recover_once(self.STREAM_ID)

        events = await self.journal().snapshot(self.STREAM_ID)
        requirements = [
            event.payload
            for event in events
            if isinstance(event.payload, CommandRecoveryRequired)
        ]
        self.assertEqual(calls, 0)
        self.assertEqual(len(requirements), 1)
        self.assertEqual(
            requirements[0].allowed_actions,
            ("abandon", "request_user"),
        )
        state = StateProjector().project(self.STREAM_ID, events).state
        self.assertIs(
            state.command(command.command_id).phase,
            CommandPhase.UNKNOWN,
        )

    async def test_current_reconcile_can_commit_indeterminate_requirement(self):
        recovery = RecoveryContract(reconcile_unknown=True)
        journal = self.journal()
        command, step_event = await self._seed_pending(
            journal,
            recovery=recovery,
        )
        await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-indeterminate",
        ))
        self.clock.advance(31)

        async def handler(_context, _arguments):
            raise AssertionError("unknown command must not be invoked")

        async def reconcile(_context, _arguments):
            return RecoveryIndeterminate("provider_unreachable")

        runtime = self._runtime(
            self.journal(),
            ToolBinding(
                handler,
                recovery=recovery,
                reconcile_unknown=reconcile,
            ),
            namespace="indeterminate",
        )
        await runtime.recover_once(self.STREAM_ID)

        events = await self.journal().snapshot(self.STREAM_ID)
        requirements = [
            event.payload
            for event in events
            if isinstance(event.payload, CommandRecoveryRequired)
        ]
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].reason, "provider_unreachable")

    async def test_running_recovery_queries_persisted_receipt(self):
        recovery = RecoveryContract(
            running_recovery=RunningRecovery.QUERY,
        )
        journal = self.journal()
        command, step_event = await self._seed_pending(
            journal,
            recovery=recovery,
        )
        dispatch = await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-running",
        ))
        await journal.record_attempt_fact(EventDraft(
            event_id="accepted-event",
            stream_id=self.STREAM_ID,
            payload=ExternalOperationAccepted(
                command_id=command.command_id,
                attempt_id=dispatch.payload.attempt_id,
                external_operation_id="remote-42",
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ))
        handler_calls = 0
        queried: list[str] = []

        async def handler(_context, _arguments):
            nonlocal handler_calls
            handler_calls += 1
            raise AssertionError("running command must not be invoked")

        async def query(_context, external_operation_id):
            queried.append(external_operation_id)
            return ToolTerminal(CommandOutcome(
                OutcomeStatus.SUCCEEDED,
                value="done",
            ))

        runtime = self._runtime(
            self.journal(),
            ToolBinding(
                handler,
                recovery=recovery,
                query_running=query,
            ),
            namespace="query",
        )
        await runtime.recover_once(self.STREAM_ID)

        self.assertEqual(handler_calls, 0)
        self.assertEqual(queried, ["remote-42"])
        events = await self.journal().snapshot(self.STREAM_ID)
        self.assertEqual(
            sum(isinstance(event.payload, CommandReconcileStarted)
                for event in events),
            1,
        )
        state = StateProjector().project(self.STREAM_ID, events).state
        self.assertIs(
            state.command(command.command_id).phase,
            CommandPhase.TERMINAL,
        )

    async def test_running_reconcile_is_single_flight_across_workers(self):
        recovery = RecoveryContract(
            running_recovery=RunningRecovery.QUERY,
        )
        journal = self.journal()
        command, step_event = await self._seed_pending(
            journal,
            recovery=recovery,
        )
        dispatch = await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-running-race",
        ))
        await journal.record_attempt_fact(EventDraft(
            event_id="accepted-race",
            stream_id=self.STREAM_ID,
            payload=ExternalOperationAccepted(
                command.command_id,
                dispatch.payload.attempt_id,
                "remote-race",
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ))
        query_started = asyncio.Event()
        release_query = asyncio.Event()
        queries: list[str] = []

        async def handler(_context, _arguments):
            raise AssertionError("running command must not be invoked")

        async def query(context, external_operation_id):
            queries.append(context.attempt_id)
            self.assertEqual(external_operation_id, "remote-race")
            query_started.set()
            await release_query.wait()
            return ToolTerminal(CommandOutcome(
                OutcomeStatus.SUCCEEDED,
                value="done",
            ))

        binding = ToolBinding(
            handler,
            recovery=recovery,
            query_running=query,
        )
        runtime_a = self._runtime(
            self.journal(),
            binding,
            namespace="query-a",
        )
        runtime_b = self._runtime(
            self.journal(),
            binding,
            namespace="query-b",
        )
        worker_a = asyncio.create_task(
            runtime_a.recover_once(self.STREAM_ID)
        )
        await asyncio.wait_for(query_started.wait(), timeout=1)
        worker_b_result = await runtime_b.recover_once(self.STREAM_ID)

        self.assertEqual(queries, ["attempt-running-race"])
        self.assertEqual(worker_b_result, ())
        release_query.set()
        await worker_a

        events = await self.journal().snapshot(self.STREAM_ID)
        self.assertEqual(
            sum(isinstance(event.payload, CommandReconcileStarted)
                for event in events),
            1,
        )
        self.assertEqual(
            sum(isinstance(event.payload, CommandOutcomeReceived)
                for event in events),
            1,
        )
        self.assertIs(
            StateProjector().project(
                self.STREAM_ID,
                events,
            ).state.command(command.command_id).phase,
            CommandPhase.TERMINAL,
        )

    async def test_expired_reconcile_cannot_commit_no_effect(self):
        recovery = RecoveryContract(reconcile_unknown=True)
        journal = self.journal()
        command, step_event = await self._seed_pending(
            journal,
            recovery=recovery,
        )
        dispatch = await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-stale-reconcile",
        ))
        self.clock.advance(31)
        reconcile_1 = await journal.start_reconcile(EventDraft(
            event_id="reconcile-event-1",
            stream_id=self.STREAM_ID,
            payload=CommandReconcileStarted(
                "reconcile-1",
                1,
                command.command_id,
                dispatch.payload.attempt_id,
                "worker-1",
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ), lease_seconds=1)
        self.clock.advance(2)
        reconcile_2 = await journal.start_reconcile(EventDraft(
            event_id="reconcile-event-2",
            stream_id=self.STREAM_ID,
            payload=CommandReconcileStarted(
                "reconcile-2",
                2,
                command.command_id,
                dispatch.payload.attempt_id,
                "worker-2",
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ), lease_seconds=10)

        stale = await journal.confirm_no_effect(EventDraft(
            event_id="stale-no-effect",
            stream_id=self.STREAM_ID,
            payload=DispatchAttemptConfirmedNoEffect(
                command.command_id,
                dispatch.payload.attempt_id,
            ),
            occurred_at=NOW,
            causation_id=reconcile_1.event_id,
        ))
        current = await journal.confirm_no_effect(EventDraft(
            event_id="current-no-effect",
            stream_id=self.STREAM_ID,
            payload=DispatchAttemptConfirmedNoEffect(
                command.command_id,
                dispatch.payload.attempt_id,
            ),
            occurred_at=NOW,
            causation_id=reconcile_2.event_id,
        ))

        self.assertIsNone(stale)
        self.assertIsNotNone(current)
        events = await journal.snapshot(self.STREAM_ID)
        no_effects = [
            event
            for event in events
            if isinstance(
                event.payload,
                DispatchAttemptConfirmedNoEffect,
            )
        ]
        self.assertEqual(no_effects, [current])
        self.assertIs(
            StateProjector().project(
                self.STREAM_ID,
                events,
            ).state.command(command.command_id).phase,
            CommandPhase.PENDING,
        )

    async def test_confirmed_no_effect_is_safely_rescheduled(self):
        recovery = RecoveryContract(reconcile_unknown=True)
        journal = self.journal()
        command, step_event = await self._seed_pending(
            journal,
            recovery=recovery,
        )
        await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-crashed",
        ))
        self.clock.advance(31)
        reconciled: list[str] = []
        invoked: list[str] = []

        async def reconcile(context, _arguments):
            reconciled.append(context.attempt_id)
            return RecoveryNoEffect()

        async def handler(context, _arguments):
            invoked.append(context.attempt_id)
            return "done"

        runtime = self._runtime(
            self.journal(),
            ToolBinding(
                handler,
                recovery=recovery,
                reconcile_unknown=reconcile,
            ),
            namespace="no-effect",
        )
        await runtime.recover_once(self.STREAM_ID)
        await runtime.dispatcher.wait_all()

        events = await self.journal().snapshot(self.STREAM_ID)
        attempts = [
            event.payload
            for event in events
            if isinstance(event.payload, DispatchAttemptStarted)
        ]
        self.assertEqual(reconciled, ["attempt-crashed"])
        self.assertEqual(len(invoked), 1)
        self.assertNotEqual(invoked[0], "attempt-crashed")
        self.assertEqual([attempt.attempt_number for attempt in attempts], [1, 2])
        self.assertEqual(
            sum(isinstance(
                event.payload,
                DispatchAttemptConfirmedNoEffect,
            ) for event in events),
            1,
        )
        self.assertIs(
            StateProjector().project(
                self.STREAM_ID,
                events,
            ).state.command(command.command_id).phase,
            CommandPhase.TERMINAL,
        )

    async def test_model_can_retry_unknown_when_contract_allows_it(self):
        recovery = RecoveryContract(retry_semantics=RetrySemantics.SAFE)
        journal = self.journal()
        command, step_event = await self._seed_pending(
            journal,
            recovery=recovery,
        )
        await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-unknown",
        ))
        self.clock.advance(31)
        invoked: list[str] = []

        async def handler(context, _arguments):
            invoked.append(context.attempt_id)
            return "retried"

        class RetryModel:
            def __init__(self) -> None:
                self.calls = 0

            async def decide(self, frame):
                self.calls += 1
                if not isinstance(
                    frame.trigger_event.payload,
                    CommandRecoveryRequired,
                ):
                    raise AssertionError("retry must consume recovery event")
                return ModelDecision(
                    content="retry safe command",
                    retry_command_ids=(command.command_id,),
                )

        model = RetryModel()
        runtime = AgentRuntime(
            self.journal(),
            model,
            {"tool": ToolBinding(handler, recovery=recovery)},
            NamespacedIds("model-retry"),
            worker_id="worker-model-retry",
        )
        await runtime.recover_once(self.STREAM_ID)
        recovery_events = await self.journal().snapshot(self.STREAM_ID)
        requirement = next(
            event.payload
            for event in recovery_events
            if isinstance(event.payload, CommandRecoveryRequired)
        )
        self.assertEqual(
            requirement.allowed_actions,
            ("retry", "abandon", "request_user"),
        )
        retry_step = await runtime.advance(self.STREAM_ID)
        await runtime.dispatcher.wait_all()

        self.assertIsNotNone(retry_step)
        self.assertEqual(
            retry_step.decision.retry_command_ids,
            (command.command_id,),
        )
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(invoked), 1)
        events = await self.journal().snapshot(self.STREAM_ID)
        attempts = [
            event.payload
            for event in events
            if isinstance(event.payload, DispatchAttemptStarted)
        ]
        self.assertEqual([attempt.attempt_number for attempt in attempts], [1, 2])
        command_state = StateProjector().project(
            self.STREAM_ID,
            events,
        ).state.command(command.command_id)
        self.assertIs(command_state.phase, CommandPhase.TERMINAL)
        self.assertTrue(command_state.attempts[0].superseded)

    async def test_frozen_retry_cannot_override_later_running_receipt(self):
        recovery = RecoveryContract(retry_semantics=RetrySemantics.SAFE)
        journal = self.journal()
        command, step_event = await self._seed_pending(
            journal,
            recovery=recovery,
        )
        dispatch = await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-receipt-race",
        ))
        self.clock.advance(31)

        async def handler(_context, _arguments):
            raise AssertionError("running receipt must block retry dispatch")

        await self._runtime(
            journal,
            ToolBinding(handler, recovery=recovery),
            namespace="requirement",
        ).recover_once(self.STREAM_ID)
        model_started = asyncio.Event()
        release_model = asyncio.Event()

        class BlockingRetryModel:
            async def decide(self, frame):
                if not isinstance(
                    frame.trigger_event.payload,
                    CommandRecoveryRequired,
                ):
                    raise AssertionError("unexpected retry trigger")
                model_started.set()
                await release_model.wait()
                return ModelDecision(
                    content="retry frozen unknown",
                    retry_command_ids=(command.command_id,),
                )

        runtime = AgentRuntime(
            self.journal(),
            BlockingRetryModel(),
            {"tool": ToolBinding(handler, recovery=recovery)},
            NamespacedIds("receipt-race"),
            worker_id="worker-receipt-race",
        )
        advance = asyncio.create_task(runtime.advance(self.STREAM_ID))
        await asyncio.wait_for(model_started.wait(), timeout=1)
        await self.journal().record_attempt_fact(EventDraft(
            event_id="late-accepted",
            stream_id=self.STREAM_ID,
            payload=ExternalOperationAccepted(
                command.command_id,
                dispatch.payload.attempt_id,
                "remote-late",
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ))
        release_model.set()
        retry_step = await advance

        self.assertEqual(
            retry_step.retry_attempts,
            ((command.command_id, "attempt-receipt-race"),),
        )
        events = await self.journal().snapshot(self.STREAM_ID)
        self.assertEqual(
            sum(isinstance(event.payload, DispatchAttemptStarted)
                for event in events),
            1,
        )
        state = StateProjector().project(self.STREAM_ID, events).state
        self.assertIs(
            state.command(command.command_id).phase,
            CommandPhase.RUNNING,
        )
        await self.journal().accept_delivery(EventDraft(
            event_id="post-race-message",
            stream_id=self.STREAM_ID,
            payload=UserMessageReceived("status?"),
            occurred_at=NOW,
            delivery=DeliveryIdentity("user", "post-race-message"),
        ))
        projection = StateProjector().project(
            self.STREAM_ID,
            await self.journal().snapshot(self.STREAM_ID),
        )
        self.assertIsNotNone(projection.next_decision)
        self.assertIs(
            projection.next_decision.state.command(
                command.command_id
            ).phase,
            CommandPhase.RUNNING,
        )

    async def test_checkpoint_can_be_deleted_and_full_replay_is_identical(self):
        journal = self.journal()
        command, step_event = await self._seed_pending(journal)
        dispatch = await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-terminal",
        ))
        await journal.record_attempt_fact(EventDraft(
            event_id="outcome-event",
            stream_id=self.STREAM_ID,
            payload=CommandOutcomeReceived(
                command_id=command.command_id,
                attempt_id=dispatch.payload.attempt_id,
                outcome=CommandOutcome(
                    OutcomeStatus.SUCCEEDED,
                    value={"answer": [1, "二"]},
                ),
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ))
        runtime = self._runtime(
            journal,
            ToolBinding(self._handler_must_not_run),
            namespace="checkpoint-a",
        )
        before_state = await runtime.state(self.STREAM_ID)
        before_turn = await runtime.turn(self.STREAM_ID)
        before_trace = await runtime.trace(self.STREAM_ID)
        self.assertEqual(self._checkpoint_count(), 1)
        events = await journal.snapshot(self.STREAM_ID)
        fingerprint = AgentRuntime._event_fingerprint(events)
        self.assertEqual(
            await journal.load_checkpoint(
                self.STREAM_ID,
                before_state.journal_position,
                fingerprint,
            ),
            before_state,
        )

        reopened_journal = self.journal()
        reopened = self._runtime(
            reopened_journal,
            ToolBinding(self._handler_must_not_run),
            namespace="checkpoint-b",
        )
        self.assertEqual(await reopened.state(self.STREAM_ID), before_state)
        await reopened_journal.delete_checkpoint(self.STREAM_ID)
        self.assertEqual(self._checkpoint_count(), 0)
        self.assertIsNone(await reopened_journal.load_checkpoint(
            self.STREAM_ID,
            before_state.journal_position,
            fingerprint,
        ))

        replayed = self._runtime(
            self.journal(),
            ToolBinding(self._handler_must_not_run),
            namespace="checkpoint-c",
        )
        self.assertEqual(await replayed.state(self.STREAM_ID), before_state)
        self.assertEqual(await replayed.turn(self.STREAM_ID), before_turn)
        self.assertEqual(await replayed.trace(self.STREAM_ID), before_trace)

    async def test_delivery_and_schema_cannot_bypass_store_contracts(self):
        step = Step(
            step_id="forged-step",
            trigger_event_id="forged-trigger",
            decision_cursor=1,
            basis_state_version="forged-basis",
            observed_journal_position=1,
            decision=ModelDecision(content="forged"),
            commands=(),
        )
        conditional = EventDraft(
            event_id="forged-step-event",
            stream_id=self.STREAM_ID,
            payload=StepCommitted(step),
            occurred_at=NOW,
            delivery=DeliveryIdentity("attacker", "delivery-1"),
        )
        for journal in (MemoryJournal(), self.journal()):
            with self.subTest(store=type(journal).__name__):
                with self.assertRaisesRegex(ValueError, "external event"):
                    await journal.accept_delivery(conditional)
                with self.assertRaisesRegex(ValueError, "conditional event"):
                    await journal.append(EventDraft(
                        event_id="message-without-delivery",
                        stream_id=self.STREAM_ID,
                        payload=UserMessageReceived("hello"),
                        occurred_at=NOW,
                    ))
                with self.assertRaisesRegex(ValueError, "schema version"):
                    await journal.accept_delivery(EventDraft(
                        event_id="future-event",
                        stream_id=self.STREAM_ID,
                        payload=UserMessageReceived("future"),
                        occurred_at=NOW,
                        schema_version=EVENT_SCHEMA_VERSION + 1,
                        delivery=DeliveryIdentity("user", "future-1"),
                    ))
                with self.assertRaisesRegex(ValueError, "attempt fact"):
                    await journal.append(EventDraft(
                        event_id="forged-attempt-fact",
                        stream_id=self.STREAM_ID,
                        payload=ExternalOperationAccepted(
                            "command",
                            "attempt",
                            "remote",
                        ),
                        occurred_at=NOW,
                        causation_id="dispatch",
                    ))
                with self.assertRaisesRegex(
                    ValueError,
                    "pending cancellation",
                ):
                    await journal.append(EventDraft(
                        event_id="forged-pending-cancel",
                        stream_id=self.STREAM_ID,
                        payload=CommandOutcomeReceived(
                            "command",
                            None,
                            CommandOutcome(OutcomeStatus.CANCELLED),
                        ),
                        occurred_at=NOW,
                        causation_id="dispatch",
                    ))
                self.assertEqual(
                    await journal.snapshot(self.STREAM_ID),
                    (),
                )

    async def test_cancelled_step_acquire_compensates_committed_claim(self):
        seed = self.journal()
        await self._accept_message(seed)
        frame = StateProjector().project(
            self.STREAM_ID,
            await seed.snapshot(self.STREAM_ID),
        ).next_decision
        request = StepClaimRequest(
            stream_id=self.STREAM_ID,
            trigger_event_id=frame.trigger_event.event_id,
            decision_cursor=frame.decision_cursor,
            basis_state_version=frame.basis_state_version,
            observed_journal_position=frame.observed_journal_position,
        )
        journal = BlockingAcquireSqliteJournal(
            self.database_path,
            clock=self.clock,
        )
        acquire = asyncio.create_task(journal.acquire_step(
            request,
            token="cancelled-claim",
            owner_id="cancelled-worker",
            lease_seconds=30,
        ))
        started = await asyncio.to_thread(
            journal.acquire_started.wait,
            1,
        )
        self.assertTrue(started)
        acquire.cancel()
        await asyncio.sleep(0)
        acquire.cancel()
        journal.release_acquire.set()
        with self.assertRaises(asyncio.CancelledError):
            await acquire

        connection = sqlite3.connect(self.database_path)
        try:
            claim_count = connection.execute(
                "SELECT COUNT(*) FROM step_claims"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(claim_count, 0)

    async def test_step_heartbeat_keeps_slow_decision_claim_alive(self):
        class SlowDecision:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def decide(self, _frame):
                self.started.set()
                await self.release.wait()
                return ModelDecision(content="slow decision completed")

        stores = (
            MemoryJournal(clock=self.clock),
            SqliteJournal(self.database_path, clock=self.clock),
        )
        for index, journal in enumerate(stores):
            with self.subTest(store=type(journal).__name__):
                decision = SlowDecision()
                runtime = AgentRuntime(
                    journal,
                    decision,
                    {},
                    NamespacedIds(f"slow-step-{index}"),
                    step_lease_seconds=0.6,
                )
                await runtime.receive_user_message(
                    self.STREAM_ID,
                    "slow",
                    delivery_id="slow-step-delivery",
                )
                advancing = asyncio.create_task(
                    runtime.advance(self.STREAM_ID)
                )
                await asyncio.wait_for(decision.started.wait(), timeout=1)
                self.clock.advance(0.35)
                await asyncio.sleep(0.3)
                self.clock.advance(0.35)
                await asyncio.sleep(0.3)
                self.clock.advance(0.35)
                decision.release.set()
                step = await advancing
                self.assertIsNotNone(step)
                self.assertEqual(step.decision.content, "slow decision completed")

    async def test_attempt_heartbeat_blocks_recovery_takeover(self):
        journal = self.journal()
        command, _ = await self._seed_pending(journal)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(_context, _arguments):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "done"

        binding = ToolBinding(handler)
        runtime = AgentRuntime(
            journal,
            ModelMustNotRun(),
            {"tool": binding},
            NamespacedIds("attempt-heartbeat"),
            attempt_lease_seconds=0.6,
        )
        await runtime.dispatcher.start_pending(self.STREAM_ID)
        await asyncio.wait_for(started.wait(), timeout=1)
        self.clock.advance(0.35)
        await asyncio.sleep(0.3)
        self.clock.advance(0.35)

        takeover = AgentRuntime(
            self.journal(),
            ModelMustNotRun(),
            {"tool": binding},
            NamespacedIds("attempt-takeover"),
            worker_id="attempt-takeover-worker",
            attempt_lease_seconds=0.6,
        )
        try:
            self.assertEqual(await takeover.recover_once(self.STREAM_ID), ())
        finally:
            release.set()
            await asyncio.gather(
                runtime.dispatcher.wait(command.command_id),
                return_exceptions=True,
            )

        events = await journal.snapshot(self.STREAM_ID)
        self.assertEqual(calls, 1)
        self.assertEqual(sum(
            isinstance(event.payload, DispatchAttemptStarted)
            for event in events
        ), 1)
        self.assertFalse(any(
            isinstance(event.payload, CommandRecoveryRequired)
            for event in events
        ))

    async def test_reconcile_heartbeat_blocks_second_reconciler(self):
        recovery = RecoveryContract(reconcile_unknown=True)
        journal = self.journal()
        command, step_event = await self._seed_pending(
            journal,
            recovery=recovery,
        )
        await journal.start_attempt(
            self._dispatch_draft(
                command,
                step_event.event_id,
                attempt_id="reconcile-heartbeat-attempt",
            ),
            lease_seconds=0.2,
        )
        self.clock.advance(0.21)
        reconcile_started = asyncio.Event()
        release_reconcile = asyncio.Event()
        calls = 0

        async def reconcile_a(_context, _arguments):
            nonlocal calls
            calls += 1
            reconcile_started.set()
            await release_reconcile.wait()
            return ToolTerminal(CommandOutcome(OutcomeStatus.SUCCEEDED))

        async def reconcile_b(_context, _arguments):
            raise AssertionError("active reconcile lease must block takeover")

        runtime_a = AgentRuntime(
            journal,
            ModelMustNotRun(),
            {"tool": ToolBinding(
                self._handler_must_not_run,
                recovery=recovery,
                reconcile_unknown=reconcile_a,
            )},
            NamespacedIds("reconcile-heartbeat-a"),
            reconcile_lease_seconds=0.6,
        )
        recovering = asyncio.create_task(
            runtime_a.recover_once(self.STREAM_ID)
        )
        await asyncio.wait_for(reconcile_started.wait(), timeout=1)
        self.clock.advance(0.35)
        await asyncio.sleep(0.3)
        self.clock.advance(0.35)
        runtime_b = AgentRuntime(
            self.journal(),
            ModelMustNotRun(),
            {"tool": ToolBinding(
                self._handler_must_not_run,
                recovery=recovery,
                reconcile_unknown=reconcile_b,
            )},
            NamespacedIds("reconcile-heartbeat-b"),
            worker_id="reconcile-heartbeat-worker-b",
            reconcile_lease_seconds=0.6,
        )
        try:
            self.assertEqual(await runtime_b.recover_once(self.STREAM_ID), ())
        finally:
            release_reconcile.set()
            await asyncio.gather(recovering, return_exceptions=True)

        events = await journal.snapshot(self.STREAM_ID)
        self.assertEqual(calls, 1)
        self.assertEqual(sum(
            isinstance(event.payload, CommandReconcileStarted)
            for event in events
        ), 1)
        self.assertIs(
            StateProjector().project(
                self.STREAM_ID,
                events,
            ).state.command(command.command_id).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )

    async def test_cancelled_snapshot_waits_for_file_handle_release(self):
        journal = BlockingSnapshotSqliteJournal(
            self.database_path,
            clock=self.clock,
        )
        snapshot = asyncio.create_task(journal.snapshot(self.STREAM_ID))
        started = await asyncio.to_thread(
            journal.snapshot_started.wait,
            1,
        )
        self.assertTrue(started)
        snapshot.cancel()
        await asyncio.sleep(0.05)
        self.assertFalse(snapshot.done())
        journal.release_snapshot.set()
        with self.assertRaises(asyncio.CancelledError):
            await snapshot

    async def test_stale_reconcile_cannot_commit_attempt_fact(self):
        recovery = RecoveryContract(
            reconcile_unknown=True,
            running_recovery=RunningRecovery.QUERY,
        )
        journal = self.journal()
        command, step_event = await self._seed_pending(
            journal,
            recovery=recovery,
        )
        dispatch = await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-fenced-fact",
        ))
        accepted = await journal.record_attempt_fact(EventDraft(
            event_id="accepted-fenced-fact",
            stream_id=self.STREAM_ID,
            payload=ExternalOperationAccepted(
                command.command_id,
                dispatch.payload.attempt_id,
                "remote-fenced-fact",
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ))
        first = await journal.start_reconcile(EventDraft(
            event_id="reconcile-event-1",
            stream_id=self.STREAM_ID,
            payload=CommandReconcileStarted(
                "reconcile-1",
                1,
                command.command_id,
                dispatch.payload.attempt_id,
                "worker-1",
            ),
            occurred_at=NOW,
            causation_id=accepted.event_id,
        ), lease_seconds=1)
        self.clock.advance(2)
        second = await journal.start_reconcile(EventDraft(
            event_id="reconcile-event-2",
            stream_id=self.STREAM_ID,
            payload=CommandReconcileStarted(
                "reconcile-2",
                2,
                command.command_id,
                dispatch.payload.attempt_id,
                "worker-2",
            ),
            occurred_at=NOW,
            causation_id=accepted.event_id,
        ), lease_seconds=10)

        stale = await journal.record_attempt_fact(EventDraft(
            event_id="stale-terminal",
            stream_id=self.STREAM_ID,
            payload=CommandOutcomeReceived(
                command.command_id,
                dispatch.payload.attempt_id,
                CommandOutcome(OutcomeStatus.FAILED),
            ),
            occurred_at=NOW,
            causation_id=first.event_id,
        ))
        current = await journal.record_attempt_fact(EventDraft(
            event_id="current-terminal",
            stream_id=self.STREAM_ID,
            payload=CommandOutcomeReceived(
                command.command_id,
                dispatch.payload.attempt_id,
                CommandOutcome(OutcomeStatus.SUCCEEDED),
            ),
            occurred_at=NOW,
            causation_id=second.event_id,
        ))

        self.assertIsNone(stale)
        self.assertIsNotNone(current)
        events = await journal.snapshot(self.STREAM_ID)
        outcomes = [
            event for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
        ]
        self.assertEqual(outcomes, [current])
        self.assertIs(
            StateProjector().project(
                self.STREAM_ID,
                events,
            ).state.command(command.command_id).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )

    async def test_late_receipt_corrects_no_effect_projection_and_index(self):
        journal = self.journal()
        command, step_event = await self._seed_pending(journal)
        dispatch = await journal.start_attempt(self._dispatch_draft(
            command,
            step_event.event_id,
            attempt_id="attempt-late-receipt",
        ))
        self.clock.advance(31)
        reconcile = await journal.start_reconcile(EventDraft(
            event_id="late-receipt-reconcile-event",
            stream_id=self.STREAM_ID,
            payload=CommandReconcileStarted(
                "late-receipt-reconcile",
                1,
                command.command_id,
                dispatch.payload.attempt_id,
                "recovery-worker",
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ), lease_seconds=10)
        no_effect = await journal.confirm_no_effect(EventDraft(
            event_id="late-receipt-no-effect",
            stream_id=self.STREAM_ID,
            payload=DispatchAttemptConfirmedNoEffect(
                command.command_id,
                dispatch.payload.attempt_id,
            ),
            occurred_at=NOW,
            causation_id=reconcile.event_id,
        ))
        accepted = await journal.record_attempt_fact(EventDraft(
            event_id="late-receipt-accepted",
            stream_id=self.STREAM_ID,
            payload=ExternalOperationAccepted(
                command.command_id,
                dispatch.payload.attempt_id,
                "remote-late-receipt",
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ))

        state = StateProjector().project(
            self.STREAM_ID,
            await journal.snapshot(self.STREAM_ID),
        ).state.command(command.command_id)
        self.assertIs(state.phase, CommandPhase.RUNNING)
        self.assertIs(state.current_attempt.phase, AttemptPhase.RUNNING)
        self.assertIsNone(state.dispatch_eligible_by_event_id)
        blocked = await journal.start_attempt(EventDraft(
            event_id="attempt-after-late-receipt-event",
            stream_id=self.STREAM_ID,
            payload=DispatchAttemptStarted(
                "attempt-after-late-receipt",
                command.command_id,
                2,
                "attempt-after-late-receipt-claim",
                "worker",
            ),
            occurred_at=NOW,
            causation_id=no_effect.event_id,
        ))
        self.assertIsNone(blocked)
        self.assertIsNotNone(accepted)
        connection = sqlite3.connect(self.database_path)
        try:
            eligibility = connection.execute(
                """
                SELECT dispatch_eligible_event_id FROM commands
                WHERE command_id = ?
                """,
                (command.command_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIsNone(eligibility)

    async def test_terminal_attempt_rejects_stale_recovery_requirement(self):
        stores = (
            MemoryJournal(clock=self.clock),
            self.journal(),
        )
        seeded = []
        for journal in stores:
            command, step_event = await self._seed_pending(journal)
            dispatch = await journal.start_attempt(self._dispatch_draft(
                command,
                step_event.event_id,
                attempt_id="attempt-terminal-recovery",
            ))
            seeded.append((journal, command, dispatch))
        self.clock.advance(31)
        for journal, command, dispatch in seeded:
            await journal.record_attempt_fact(EventDraft(
                event_id="terminal-before-recovery",
                stream_id=self.STREAM_ID,
                payload=CommandOutcomeReceived(
                    command.command_id,
                    dispatch.payload.attempt_id,
                    CommandOutcome(OutcomeStatus.SUCCEEDED),
                ),
                occurred_at=NOW,
                causation_id=dispatch.event_id,
            ))
            result = await journal.ensure_recovery_required(EventDraft(
                event_id="stale-recovery-requirement",
                stream_id=self.STREAM_ID,
                payload=CommandRecoveryRequired(
                    command.command_id,
                    dispatch.payload.attempt_id,
                    "stale",
                    ("abandon",),
                ),
                occurred_at=NOW,
                causation_id=dispatch.event_id,
            ))
            self.assertIsNone(result)
            self.assertFalse(any(
                isinstance(event.payload, CommandRecoveryRequired)
                for event in await journal.snapshot(self.STREAM_ID)
            ))

    async def test_conditional_internal_event_cannot_claim_delivery(self):
        for journal in (MemoryJournal(clock=self.clock), self.journal()):
            with self.subTest(store=type(journal).__name__):
                command, step_event = await self._seed_pending(journal)
                delivery = DeliveryIdentity("provider", "shared-delivery")
                draft = self._dispatch_draft(
                    command,
                    step_event.event_id,
                    attempt_id="attempt-with-delivery",
                )
                draft = EventDraft(
                    event_id=draft.event_id,
                    stream_id=draft.stream_id,
                    payload=draft.payload,
                    occurred_at=draft.occurred_at,
                    causation_id=draft.causation_id,
                    delivery=delivery,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "internal event",
                ):
                    await journal.start_attempt(draft)
                accepted = await journal.accept_delivery(EventDraft(
                    event_id="external-shared-delivery",
                    stream_id=self.STREAM_ID,
                    payload=UserInterruptReceived("external"),
                    occurred_at=NOW,
                    delivery=delivery,
                ))
                self.assertTrue(accepted.inserted)

    async def test_memory_pending_cancel_pair_rolls_back_on_schema_error(self):
        journal = MemoryJournal(clock=self.clock)
        target, cancel, cancel_step = await self._seed_pending_cancel(journal)
        dispatch = await journal.start_attempt(self._dispatch_draft(
            cancel,
            cancel_step.event_id,
            attempt_id="cancel-attempt-schema",
        ))
        target_draft = EventDraft(
            event_id="target-cancel-schema",
            stream_id=self.STREAM_ID,
            payload=CommandOutcomeReceived(
                target.command_id,
                None,
                CommandOutcome(OutcomeStatus.CANCELLED),
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        )
        cancel_draft = EventDraft(
            event_id="cancel-terminal-schema",
            stream_id=self.STREAM_ID,
            payload=CommandOutcomeReceived(
                cancel.command_id,
                dispatch.payload.attempt_id,
                CommandOutcome(OutcomeStatus.SUCCEEDED, value="cancelled"),
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
            schema_version=EVENT_SCHEMA_VERSION + 1,
        )
        before = await journal.snapshot(self.STREAM_ID)
        with self.assertRaisesRegex(ValueError, "schema version"):
            await journal.commit_pending_cancellation(
                target_draft,
                cancel_draft,
            )
        self.assertEqual(await journal.snapshot(self.STREAM_ID), before)

    async def test_cancel_attempt_recovers_before_pending_target_dispatch(self):
        journal = self.journal()
        target, cancel, cancel_step = await self._seed_pending_cancel(journal)
        dispatch = await journal.start_attempt(self._dispatch_draft(
            cancel,
            cancel_step.event_id,
            attempt_id="crashed-cancel-attempt",
        ))
        self.clock.advance(31)
        runtime = self._runtime(
            self.journal(),
            ToolBinding(self._handler_must_not_run),
            namespace="cancel-recovery",
        )

        await runtime.recover_once(self.STREAM_ID)

        events = await journal.snapshot(self.STREAM_ID)
        state = StateProjector().project(self.STREAM_ID, events).state
        self.assertIs(
            state.command(target.command_id).outcome.status,
            OutcomeStatus.CANCELLED,
        )
        self.assertIs(
            state.command(cancel.command_id).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )
        self.assertEqual(state.command(cancel.command_id).outcome.value, "cancelled")
        self.assertFalse(any(
            isinstance(event.payload, DispatchAttemptStarted)
            and event.payload.command_id == target.command_id
            for event in events
        ))
        target_outcome = next(
            event for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
            and event.payload.command_id == target.command_id
        )
        cause = next(
            event for event in events
            if event.event_id == target_outcome.causation_id
        )
        self.assertIsInstance(cause.payload, CommandReconcileStarted)
        self.assertEqual(cause.payload.attempt_id, dispatch.payload.attempt_id)

    async def test_recovered_cancel_succeeds_when_target_cancelled_after_request(self):
        journal = self.journal()
        target, cancel, cancel_step = await self._seed_pending_cancel(journal)
        target_dispatch = await journal.start_attempt(self._dispatch_draft(
            target,
            "step-event",
            attempt_id="target-before-cancel",
        ))
        cancel_dispatch = await journal.start_attempt(self._dispatch_draft(
            cancel,
            cancel_step.event_id,
            attempt_id="cancel-before-crash",
        ))
        await journal.record_attempt_fact(EventDraft(
            event_id="target-cancelled-after-request",
            stream_id=self.STREAM_ID,
            payload=CommandOutcomeReceived(
                target.command_id,
                target_dispatch.payload.attempt_id,
                CommandOutcome(OutcomeStatus.CANCELLED),
            ),
            occurred_at=NOW,
            causation_id=target_dispatch.event_id,
        ))
        self.clock.advance(31)
        runtime = self._runtime(
            self.journal(),
            ToolBinding(self._handler_must_not_run),
            namespace="cancel-crash-window",
        )

        await runtime.recover_once(self.STREAM_ID)

        state = await runtime.state(self.STREAM_ID)
        cancel_state = state.command(cancel.command_id)
        self.assertIs(cancel_state.outcome.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(cancel_state.outcome.value, "cancelled")
        self.assertEqual(
            cancel_state.current_attempt.attempt_id,
            cancel_dispatch.payload.attempt_id,
        )

    async def test_pending_cancel_after_no_effect_prevents_second_attempt(self):
        journal = self.journal()
        target, step_event = await self._seed_pending(journal)
        dispatch = await journal.start_attempt(self._dispatch_draft(
            target,
            step_event.event_id,
            attempt_id="target-no-effect-attempt",
        ))
        self.clock.advance(31)
        reconcile = await journal.start_reconcile(EventDraft(
            event_id="target-no-effect-reconcile-event",
            stream_id=self.STREAM_ID,
            payload=CommandReconcileStarted(
                "target-no-effect-reconcile",
                1,
                target.command_id,
                dispatch.payload.attempt_id,
                "recovery-worker",
            ),
            occurred_at=NOW,
            causation_id=dispatch.event_id,
        ), lease_seconds=10)
        await journal.confirm_no_effect(EventDraft(
            event_id="target-no-effect",
            stream_id=self.STREAM_ID,
            payload=DispatchAttemptConfirmedNoEffect(
                target.command_id,
                dispatch.payload.attempt_id,
            ),
            occurred_at=NOW,
            causation_id=reconcile.event_id,
        ))
        cancel, _ = await self._commit_cancel_step(journal, target)
        runtime = self._runtime(
            self.journal(),
            ToolBinding(self._handler_must_not_run),
            namespace="cancel-no-effect",
        )

        started = await runtime.dispatcher.start_pending(self.STREAM_ID)
        await runtime.dispatcher.wait(cancel.command_id)

        self.assertEqual(started, (cancel.command_id,))
        state = await runtime.state(self.STREAM_ID)
        self.assertIs(
            state.command(target.command_id).outcome.status,
            OutcomeStatus.CANCELLED,
        )
        self.assertEqual(len(state.command(target.command_id).attempts), 1)

    async def test_pending_cancel_and_target_dispatch_are_atomic(self):
        journal = self.journal()
        target, cancel, cancel_step = await self._seed_pending_cancel(journal)
        cancel_dispatch = await journal.start_attempt(self._dispatch_draft(
            cancel,
            cancel_step.event_id,
            attempt_id="cancel-race-attempt",
        ))
        cause = cancel_dispatch.event_id
        target_cancel = EventDraft(
            event_id="target-race-cancel",
            stream_id=self.STREAM_ID,
            payload=CommandOutcomeReceived(
                target.command_id,
                None,
                CommandOutcome(OutcomeStatus.CANCELLED),
            ),
            occurred_at=NOW,
            causation_id=cause,
        )
        cancel_terminal = EventDraft(
            event_id="cancel-race-terminal",
            stream_id=self.STREAM_ID,
            payload=CommandOutcomeReceived(
                cancel.command_id,
                cancel_dispatch.payload.attempt_id,
                CommandOutcome(OutcomeStatus.SUCCEEDED, value="cancelled"),
            ),
            occurred_at=NOW,
            causation_id=cause,
        )
        target_dispatch = self._dispatch_draft(
            target,
            "step-event",
            attempt_id="target-race-attempt",
        )

        cancellation, started = await asyncio.gather(
            self.journal().commit_pending_cancellation(
                target_cancel,
                cancel_terminal,
            ),
            self.journal().start_attempt(target_dispatch),
        )

        self.assertNotEqual(cancellation is None, started is None)
        state = StateProjector().project(
            self.STREAM_ID,
            await journal.snapshot(self.STREAM_ID),
        ).state
        if cancellation is not None:
            self.assertIs(
                state.command(target.command_id).outcome.status,
                OutcomeStatus.CANCELLED,
            )
        else:
            self.assertIs(
                state.command(target.command_id).phase,
                CommandPhase.UNKNOWN,
            )

    async def test_persisted_recovery_contract_cannot_silently_change(self):
        journal = self.journal()
        await self._seed_pending(journal)

        async def handler(_context, _arguments):
            return "done"

        changed = RecoveryContract(retry_semantics=RetrySemantics.SAFE)
        runtime = self._runtime(
            self.journal(),
            ToolBinding(handler, recovery=changed),
            namespace="changed-contract",
        )
        with self.assertRaisesRegex(ValueError, "contract changed"):
            await runtime.dispatcher.start_pending(self.STREAM_ID)
        events = await journal.snapshot(self.STREAM_ID)
        self.assertFalse(any(
            isinstance(event.payload, DispatchAttemptStarted)
            for event in events
        ))

    def test_unknown_database_schema_is_not_downgraded(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA user_version = 999")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(ValueError, "database schema version"):
            self.journal()

        connection = sqlite3.connect(self.database_path)
        try:
            version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, 999)

    def test_legacy_schema_version_is_rejected(self):
        self.journal()
        connection = sqlite3.connect(self.database_path)
        try:
            version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(version, 2)

        with self.assertRaisesRegex(ValueError, "database schema version"):
            self.journal()

    async def _accept_message(self, journal: SqliteJournal) -> None:
        await journal.accept_delivery(EventDraft(
            event_id="message-event",
            stream_id=self.STREAM_ID,
            payload=UserMessageReceived("run tool"),
            occurred_at=NOW,
            delivery=DeliveryIdentity("user", "message-delivery"),
        ))

    async def _seed_pending(
        self,
        journal: SqliteJournal,
        *,
        recovery: RecoveryContract = RecoveryContract(),
    ) -> tuple[Command, object]:
        await self._accept_message(journal)
        frame = StateProjector().project(
            self.STREAM_ID,
            await journal.snapshot(self.STREAM_ID),
        ).next_decision
        effect = InvokeTool("tool", (("city", "成都"),))
        decision = ModelDecision(
            content="start tool",
            command_requests=(effect,),
        )
        command = Command(
            command_id="command-1",
            effect=effect,
            recovery=recovery,
            idempotency_key=(
                "command-1"
                if recovery.retry_semantics
                is RetrySemantics.IDEMPOTENCY_KEY_REQUIRED
                else None
            ),
        )
        step = Step(
            step_id="step-1",
            trigger_event_id=frame.trigger_event.event_id,
            decision_cursor=frame.decision_cursor,
            basis_state_version=frame.basis_state_version,
            observed_journal_position=frame.observed_journal_position,
            decision=decision,
            commands=(command,),
        )
        request = StepClaimRequest(
            stream_id=self.STREAM_ID,
            trigger_event_id=frame.trigger_event.event_id,
            decision_cursor=frame.decision_cursor,
            basis_state_version=frame.basis_state_version,
            observed_journal_position=frame.observed_journal_position,
        )
        lease = await journal.acquire_step(
            request,
            token="seed-claim",
            owner_id="seed-worker",
            lease_seconds=30,
        )
        event = await journal.commit_step(lease, EventDraft(
            event_id="step-event",
            stream_id=self.STREAM_ID,
            payload=StepCommitted(step),
            occurred_at=NOW,
            causation_id=frame.trigger_event.event_id,
        ))
        return command, event

    async def _commit_cancel_step(
        self,
        journal,
        target: Command,
        *,
        abandon_target: bool = False,
    ) -> tuple[Command, object]:
        projection = StateProjector().project(
            self.STREAM_ID,
            await journal.snapshot(self.STREAM_ID),
        )
        if projection.next_decision is None:
            await journal.accept_delivery(EventDraft(
                event_id="cancel-request-event",
                stream_id=self.STREAM_ID,
                payload=UserInterruptReceived("cancel target"),
                occurred_at=NOW,
                delivery=DeliveryIdentity(
                    "user",
                    "cancel-request-delivery",
                ),
            ))
            projection = StateProjector().project(
                self.STREAM_ID,
                await journal.snapshot(self.STREAM_ID),
            )
        frame = projection.next_decision
        effect = CancelTool(target.command_id)
        decision = ModelDecision(
            content="cancel target",
            command_requests=(effect,),
            abandon_command_ids=(
                (target.command_id,) if abandon_target else ()
            ),
        )
        command = Command("cancel-command", effect)
        step = Step(
            step_id="cancel-step",
            trigger_event_id=frame.trigger_event.event_id,
            decision_cursor=frame.decision_cursor,
            basis_state_version=frame.basis_state_version,
            observed_journal_position=frame.observed_journal_position,
            decision=decision,
            commands=(command,),
        )
        request = StepClaimRequest(
            stream_id=self.STREAM_ID,
            trigger_event_id=frame.trigger_event.event_id,
            decision_cursor=frame.decision_cursor,
            basis_state_version=frame.basis_state_version,
            observed_journal_position=frame.observed_journal_position,
        )
        lease = await journal.acquire_step(
            request,
            token="cancel-step-claim",
            owner_id="cancel-step-worker",
            lease_seconds=30,
        )
        event = await journal.commit_step(lease, EventDraft(
            event_id="cancel-step-event",
            stream_id=self.STREAM_ID,
            payload=StepCommitted(step),
            occurred_at=NOW,
            causation_id=frame.trigger_event.event_id,
        ))
        return command, event

    async def _seed_pending_cancel(
        self,
        journal,
        *,
        abandon_target: bool = False,
    ) -> tuple[Command, Command, object]:
        target, _ = await self._seed_pending(journal)
        cancel, cancel_step = await self._commit_cancel_step(
            journal,
            target,
            abandon_target=abandon_target,
        )
        return target, cancel, cancel_step

    def _dispatch_draft(
        self,
        command: Command,
        causation_id: str,
        *,
        attempt_id: str,
    ) -> EventDraft:
        return EventDraft(
            event_id=f"{attempt_id}-event",
            stream_id=self.STREAM_ID,
            payload=DispatchAttemptStarted(
                attempt_id=attempt_id,
                command_id=command.command_id,
                attempt_number=1,
                claim_token=f"{attempt_id}-claim",
                worker_id="crashed-worker",
            ),
            occurred_at=NOW,
            causation_id=causation_id,
        )

    def _step_draft(
        self,
        frame,
        *,
        event_id: str,
        step_id: str,
    ) -> EventDraft:
        step = Step(
            step_id=step_id,
            trigger_event_id=frame.trigger_event.event_id,
            decision_cursor=frame.decision_cursor,
            basis_state_version=frame.basis_state_version,
            observed_journal_position=frame.observed_journal_position,
            decision=ModelDecision(content="done"),
            commands=(),
        )
        return EventDraft(
            event_id=event_id,
            stream_id=self.STREAM_ID,
            payload=StepCommitted(step),
            occurred_at=NOW,
            causation_id=frame.trigger_event.event_id,
        )

    def _runtime(
        self,
        journal: SqliteJournal,
        binding: ToolBinding,
        *,
        namespace: str,
    ) -> AgentRuntime:
        return AgentRuntime(
            journal,
            ModelMustNotRun(),
            {"tool": binding},
            NamespacedIds(namespace),
            worker_id=f"worker-{namespace}",
        )

    def _checkpoint_count(self) -> int:
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute(
                "SELECT COUNT(*) FROM checkpoints"
            ).fetchone()[0]
        finally:
            connection.close()

    @staticmethod
    async def _handler_must_not_run(_context, _arguments):
        raise AssertionError("tool must not run")


class DurableCodecContractTest(unittest.TestCase):
    def test_event_schema_rejects_mutable_and_extended_nodes(self):
        @dataclass(frozen=True)
        class ExtendedMessage(UserMessageReceived):
            extra: list[int]

        with self.assertRaises(TypeError):
            UserMessageReceived(["mutable"])
        with self.assertRaises(TypeError):
            UserInterruptReceived(["mutable"])
        with self.assertRaisesRegex(TypeError, "artifact refs"):
            EventDraft(
                event_id="mutable-artifacts",
                stream_id="stream-1",
                payload=UserMessageReceived("hello"),
                occurred_at=NOW,
                artifact_refs=["artifact-1"],
            )
        with self.assertRaisesRegex(TypeError, "payload type"):
            EventDraft(
                event_id="extended-payload",
                stream_id="stream-1",
                payload=ExtendedMessage("hello", [1]),
                occurred_at=NOW,
            )
        with self.assertRaisesRegex(ValueError, "retry attempt ids"):
            Step(
                step_id="step-closed",
                trigger_event_id="trigger-closed",
                decision_cursor=1,
                basis_state_version="basis",
                observed_journal_position=1,
                decision=ModelDecision(
                    content="retry",
                    retry_command_ids=("command-closed",),
                ),
                commands=(),
                retry_attempts=(("command-closed", object()),),
            )

    def test_event_time_must_be_utc_for_durable_round_trip(self):
        with self.assertRaisesRegex(ValueError, "UTC"):
            EventDraft(
                event_id="event-offset",
                stream_id="stream-1",
                payload=UserMessageReceived("hello"),
                occurred_at=datetime(
                    2026,
                    8,
                    21,
                    18,
                    0,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            )

    def test_all_payloads_round_trip_with_stable_tags(self):
        recovery = RecoveryContract(
            retry_semantics=RetrySemantics.IDEMPOTENCY_KEY_REQUIRED,
            reconcile_unknown=True,
            running_recovery=RunningRecovery.QUERY,
        )
        effect = InvokeTool("weather", (("city", {"name": "成都"}),))
        command = Command("command-1", effect, recovery, "command-1")
        step = Step(
            step_id="step-1",
            trigger_event_id="message-1",
            decision_cursor=1,
            basis_state_version="basis",
            observed_journal_position=1,
            decision=ModelDecision(
                content="run",
                command_requests=(effect,),
            ),
            commands=(command,),
        )
        outcome = CommandOutcome(
            OutcomeStatus.FAILED,
            value={"retryable": False},
            error_type="RemoteError",
            error_message="failed",
        )
        payloads = (
            UserMessageReceived("hello"),
            UserInterruptReceived("changed"),
            StepCommitted(step),
            CommandAuthorized("command-1"),
            CommandRejected("command-1"),
            DispatchAttemptStarted(
                "attempt-1", "command-1", 1, "claim-1", "worker-1"
            ),
            CommandReconcileStarted(
                "reconcile-1", 1, "command-1", "attempt-1", "worker-2"
            ),
            ExternalOperationAccepted(
                "command-1", "attempt-1", "remote-1"
            ),
            DispatchAttemptConfirmedNoEffect("command-1", "attempt-1"),
            CommandRecoveryRequired(
                "command-1",
                "attempt-1",
                "indeterminate",
                ("retry", "abandon"),
            ),
            CommandOutcomeReceived("command-1", "attempt-1", outcome),
            TerminationRequested("host stop"),
            RuntimeCompleted("step-event-1"),
            RuntimeTerminated("stop-event-1", ("command-1",)),
            DomainFactCommitted(
                fact_type="test.fact.v1",
                data={"value": "domain-owned"},
                requests_decision=True,
            ),
        )
        expected_tags = {
            "user.message.received",
            "user.interrupt.received",
            "step.committed",
            "command.authorized",
            "command.rejected",
            "command.attempt.started",
            "command.reconcile.started",
            "command.external.accepted",
            "command.attempt.no_effect",
            "command.recovery.required",
            "command.outcome.received",
            "runtime.termination.requested",
            "runtime.completed",
            "runtime.terminated",
            "domain.fact.committed",
        }

        encoded = [encode_payload(payload) for payload in payloads]

        self.assertEqual({tag for tag, _ in encoded}, expected_tags)
        self.assertEqual(
            tuple(
                decode_payload(tag, EVENT_SCHEMA_VERSION, data)
                for tag, data in encoded
            ),
            payloads,
        )

    def test_unknown_schema_and_fields_fail_without_losing_facts(self):
        tag, payload_json = encode_payload(UserMessageReceived("hello"))
        with self.assertRaisesRegex(ValueError, "schema version"):
            decode_payload(tag, EVENT_SCHEMA_VERSION + 1, payload_json)

        data = json.loads(payload_json)
        data["future_fact"] = "must not disappear"
        with self.assertRaisesRegex(ValueError, "fields"):
            decode_payload(
                tag,
                EVENT_SCHEMA_VERSION,
                json.dumps(data, ensure_ascii=False),
            )

    def test_delivery_fingerprint_has_versioned_golden_value(self):
        draft = EventDraft(
            event_id="ignored-event-id",
            stream_id="stream-1",
            payload=UserMessageReceived("你好"),
            occurred_at=NOW,
            causation_id="cause-1",
            correlation_id="correlation-1",
            artifact_refs=("artifact-1",),
            delivery=DeliveryIdentity("user", "delivery-1"),
        )

        self.assertEqual(
            delivery_fingerprint(draft),
            "v1:86309430b017562b818f72ffb2d344b42ce2aadda078222ff23d3bbe547be884",
        )
