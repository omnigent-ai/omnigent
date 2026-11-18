"""Pure and synthetic asyncio coverage; no server or Kiro fixtures are requested."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import FunctionType
from unittest.mock import patch

from tests.server.integration import test_kiro_permission_delivery as delivery


class TestKiroReadinessEvidence(unittest.TestCase):
    def test_private_arguments_and_unknown_metadata_do_not_affect_evidence(self) -> None:
        async def pending(_private):
            await asyncio.sleep(0)

        outputs = []
        for private in ("synthetic-private-a", "synthetic-private-雪🚀"):
            known = FunctionType(
                pending.__code__.replace(
                    co_filename=str(
                        Path(delivery.__file__).parents[3]
                        / "omnigent/server/routes/sessions/routes_core.py"
                    ),
                    co_name="get_session",
                    co_qualname=private,
                ),
                globals(),
            )(_private=private)
            unknown = FunctionType(
                pending.__code__.replace(co_filename=private, co_name=private), globals()
            )(_private=private)
            try:
                output = delivery._kiro_readiness_evidence([known, unknown, object()])
                self.assertNotIn("synthetic-private", output)
                outputs.append(output)
            finally:
                known.close()
                unknown.close()
        self.assertEqual(outputs[0], outputs[1])
        evidence = json.loads(outputs[0])
        self.assertEqual(evidence["coroutines_scanned"], 3)
        self.assertEqual(len(evidence["phases"]), 1)
        self.assertEqual(evidence["phases"][0]["phase"], "session-snapshot-route")
        self.assertEqual(
            evidence["phases"][0]["path"], "omnigent/server/routes/sessions/routes_core.py"
        )
        self.assertGreater(evidence["phases"][0]["line"], 0)

    def test_coroutine_row_and_ascii_byte_limits(self) -> None:
        async def pending():
            await asyncio.sleep(0)

        coroutines = [
            FunctionType(
                pending.__code__.replace(
                    co_filename=str(
                        Path(delivery.__file__).parents[3]
                        / "omnigent/server/routes/sessions/routes_core.py"
                    ),
                    co_name="get_session",
                    co_firstlineno=1000 + index,
                ),
                globals(),
            )()
            for index in range(40)
        ]
        try:
            output = delivery._kiro_readiness_evidence(coroutines)
            evidence = json.loads(output)
            self.assertEqual(evidence["coroutines_scanned"], 32)
            self.assertTrue(evidence["coroutine_limit_reached"])
            self.assertTrue(evidence["phase_limit_reached"])
            self.assertEqual(len(evidence["phases"]), 8)
            self.assertTrue(output.isascii())
            self.assertLessEqual(len(output.encode("ascii")), 4096)
        finally:
            for coroutine in coroutines:
                coroutine.close()

    def test_deep_unknown_await_chains_are_bounded(self) -> None:
        def chain(inner):
            yield from inner

        for links, limited in ((48, False), (49, True), (64, True)):
            with self.subTest(links=links):
                current = iter([None])
                for _ in range(links):
                    current = chain(current)
                next(current)
                try:
                    evidence = json.loads(delivery._kiro_readiness_evidence([current]))
                    self.assertEqual(evidence["await_depth_limit_reached"], limited)
                    self.assertEqual(evidence["phases"], [])
                finally:
                    current.close()


class TestKiroReadinessTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_real_deadline_captures_separate_inner_task_before_cancellation(self) -> None:
        async def pending():
            await asyncio.Event().wait()

        inner = FunctionType(
            pending.__code__.replace(
                co_filename=str(
                    Path(delivery.__file__).parents[3]
                    / "omnigent/server/routes/sessions/routes_core.py"
                ),
                co_name="get_session",
            ),
            globals(),
        )
        task = asyncio.create_task(inner(), name="synthetic-private-task")
        timeout = asyncio.timeout(0.02)
        try:
            with self.assertRaises(TimeoutError) as caught:
                async with delivery._kiro_readiness_timeout(timeout):
                    try:
                        await asyncio.Event().wait()
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(timeout.expired())
            self.assertIsInstance(caught.exception.__cause__, asyncio.CancelledError)
            evidence = json.loads(caught.exception.__notes__[0])
            self.assertEqual(evidence["phases"][0]["phase"], "session-snapshot-route")
            self.assertNotIn("synthetic-private", caught.exception.__notes__[0])
            self.assertTrue(task.cancelled())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_success_and_body_error_cancel_observer_and_keep_error_identity(self) -> None:
        for failure in (None, TimeoutError("synthetic-private-error"), ValueError()):
            with self.subTest(failure_type=type(failure).__name__):
                with patch.object(delivery, "_kiro_readiness_evidence") as observe:
                    timeout = asyncio.timeout(0.02)
                    try:
                        async with delivery._kiro_readiness_timeout(timeout):
                            if failure is not None:
                                raise failure
                    except Exception as error:
                        self.assertIs(error, failure)
                        self.assertFalse(hasattr(error, "__notes__"))
                    else:
                        self.assertIsNone(failure)
                    await asyncio.sleep(0.03)
                    observe.assert_not_called()
                    self.assertFalse(timeout.expired())

    async def test_external_cancellation_is_preserved_and_observer_cancelled(self) -> None:
        entered = asyncio.Event()

        async def subject():
            async with delivery._kiro_readiness_timeout(asyncio.timeout(0.02)):
                entered.set()
                await asyncio.Event().wait()

        with patch.object(delivery, "_kiro_readiness_evidence") as observe:
            task = asyncio.create_task(subject())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.03)
            observe.assert_not_called()

    async def test_observation_failure_cannot_replace_original_timeout(self) -> None:
        with patch.object(
            delivery, "_kiro_readiness_evidence", side_effect=RuntimeError("synthetic-private")
        ):
            with self.assertRaises(TimeoutError) as caught:
                async with delivery._kiro_readiness_timeout(asyncio.timeout(0.02)):
                    await asyncio.Event().wait()
        self.assertEqual(
            caught.exception.__notes__,
            ['{"diagnostic":"kiro-readiness-observation-unavailable"}'],
        )
        self.assertIsInstance(caught.exception.__cause__, asyncio.CancelledError)
