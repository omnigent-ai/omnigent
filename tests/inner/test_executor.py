"""Tests for the Executor interface and MockExecutor."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.inner.executor import (
    MockExecutor,
    TextChunk,
    ToolCallRequest,
    TurnComplete,
    describe_exception,
    split_transient_tail,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestMockExecutor(unittest.TestCase):
    def test_simple_response(self):
        async def _t():
            ex = MockExecutor()
            ex.enqueue_response("Hello!")
            events = [e async for e in ex.run_turn([], [], "")]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], TurnComplete)
            self.assertEqual(events[0].response, "Hello!")

        _run(_t())

    def test_tool_call(self):
        async def _t():
            ex = MockExecutor()
            ex.enqueue_tool_call("sql", {"q": "SELECT 1"})
            events = [e async for e in ex.run_turn([], [], "")]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ToolCallRequest)
            self.assertEqual(events[0].name, "sql")

        _run(_t())

    def test_tool_call_with_followup(self):
        async def _t():
            ex = MockExecutor()
            ex.enqueue_tool_call("s", {"q": "t"}, follow_up_response="Found!")
            e1 = [e async for e in ex.run_turn([], [], "")]
            self.assertIsInstance(e1[0], ToolCallRequest)
            e2 = [e async for e in ex.run_turn([], [], "")]
            self.assertIsInstance(e2[0], TurnComplete)
            self.assertEqual(e2[0].response, "Found!")

        _run(_t())

    def test_multiple_turns(self):
        async def _t():
            ex = MockExecutor()
            ex.enqueue_response("First")
            ex.enqueue_response("Second")
            e1 = [e async for e in ex.run_turn([], [], "")]
            self.assertEqual(e1[0].response, "First")
            e2 = [e async for e in ex.run_turn([], [], "")]
            self.assertEqual(e2[0].response, "Second")

        _run(_t())

    def test_empty_queue_fallback(self):
        async def _t():
            events = [e async for e in MockExecutor().run_turn([], [], "")]
            self.assertEqual(len(events), 1)
            self.assertIn("no more", events[0].response)

        _run(_t())

    def test_custom_events(self):
        async def _t():
            ex = MockExecutor()
            ex.enqueue_events(
                [TextChunk(text="Hi "), TextChunk(text="W"), TurnComplete(response="Hi W")]
            )
            events = [e async for e in ex.run_turn([], [], "")]
            self.assertEqual(len(events), 3)
            self.assertIsInstance(events[0], TextChunk)
            self.assertIsInstance(events[2], TurnComplete)

        _run(_t())


class TestSplitTransientTail(unittest.TestCase):
    def test_empty_list(self):
        split = split_transient_tail([])
        self.assertEqual(split.persisted, [])
        self.assertEqual(split.transient, [])

    def test_no_transient_tail(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        split = split_transient_tail(messages)
        self.assertEqual(split.persisted, messages)
        self.assertEqual(split.transient, [])

    def test_single_transient_at_tail(self):
        persisted_msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        notice = {
            "role": "user",
            "content": "[SYSTEM] There is 1 unread inbox item available.",
            "metadata": {"framework": "inbox_notice"},
        }
        split = split_transient_tail([*persisted_msgs, notice])
        self.assertEqual(split.persisted, persisted_msgs)
        self.assertEqual(split.transient, [notice])

    def test_multiple_transient_at_tail(self):
        persisted_msgs = [{"role": "user", "content": "hi"}]
        n1 = {"role": "user", "content": "a", "metadata": {"framework": "x"}}
        n2 = {"role": "user", "content": "b", "metadata": {"framework": "y"}}
        split = split_transient_tail([*persisted_msgs, n1, n2])
        self.assertEqual(split.persisted, persisted_msgs)
        self.assertEqual(split.transient, [n1, n2])

    def test_framework_message_in_middle_stays_persisted(self):
        # Only trailing framework messages are treated as transient.
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": "mid", "metadata": {"framework": "x"}},
            {"role": "assistant", "content": "answer"},
        ]
        split = split_transient_tail(msgs)
        self.assertEqual(split.persisted, msgs)
        self.assertEqual(split.transient, [])

    def test_missing_metadata_key_is_persisted(self):
        msgs = [{"role": "user", "content": "hi"}]
        split = split_transient_tail(msgs)
        self.assertEqual(split.persisted, msgs)
        self.assertEqual(split.transient, [])


class TestDescribeException(unittest.TestCase):
    def test_plain_exception_returns_str(self):
        exc = ValueError("bad value")
        self.assertEqual(describe_exception(exc), "bad value")

    def test_empty_str_falls_back_to_repr(self):
        # A bare RuntimeError() has an empty str().
        exc = RuntimeError()
        result = describe_exception(exc)
        self.assertIn("RuntimeError", result)
        self.assertTrue(result)

    def test_oserror_with_filename_uses_str(self):
        # FileNotFoundError with a filename already includes it in str().
        exc = FileNotFoundError(2, "No such file or directory", "/missing/file")
        result = describe_exception(exc)
        self.assertIn("/missing/file", result)
        self.assertIn("[Errno 2]", result)

    def test_oserror_without_filename_appends_cwd(self):
        # OSError without a filename (e.g. from os.getcwd() on a deleted cwd)
        # should append the current cwd so operators can identify the location.
        exc = OSError(2, "No such file or directory")
        self.assertIsNone(exc.filename)
        result = describe_exception(exc)
        self.assertIn("[Errno 2] No such file or directory", result)
        self.assertIn("cwd:", result)

    def test_oserror_without_filename_cwd_inaccessible(self):
        # When getcwd() itself fails, the description mentions "inaccessible".
        import unittest.mock

        exc = OSError(2, "No such file or directory")
        with unittest.mock.patch("os.getcwd", side_effect=OSError(2, "No such file or directory")):
            result = describe_exception(exc)
        self.assertIn("[Errno 2] No such file or directory", result)
        self.assertIn("inaccessible", result)

    def test_oserror_with_both_filenames_uses_str(self):
        # rename(src, dst) raises OSError with both filename and filename2.
        exc = OSError(2, "No such file or directory")
        exc.filename = "/src"
        exc.filename2 = "/dst"
        # str(exc) may or may not include filenames when set post-construction,
        # but describe_exception must NOT try to call getcwd() in this case.
        import unittest.mock

        with unittest.mock.patch("os.getcwd") as mock_cwd:
            result = describe_exception(exc)
            mock_cwd.assert_not_called()
        # The result should still be a non-empty string.
        self.assertTrue(result)

    def test_oserror_with_only_filename2_uses_str(self):
        exc = OSError(2, "No such file or directory")
        exc.filename2 = "/dst"
        import unittest.mock

        with unittest.mock.patch("os.getcwd") as mock_cwd:
            result = describe_exception(exc)
            mock_cwd.assert_not_called()
        self.assertTrue(result)

    def test_permission_error_without_filename_not_augmented(self):
        # A path-less PermissionError is NOT ENOENT; cwd context would mislead.
        # describe_exception must return str(exc) unchanged without calling getcwd().
        import unittest.mock

        exc = PermissionError(13, "Permission denied")
        self.assertIsNone(exc.filename)
        with unittest.mock.patch("os.getcwd") as mock_cwd:
            result = describe_exception(exc)
            mock_cwd.assert_not_called()
        self.assertEqual(result, str(exc))
        self.assertNotIn("cwd:", result)


if __name__ == "__main__":
    unittest.main()
