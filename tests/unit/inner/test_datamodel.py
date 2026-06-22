"""Tests for the Omnigent datamodel module."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.inner.datamodel import (
    AgentDef,
    Connection,
    Credentials,
    History,
    Memory,
    MemoryConfig,
    Message,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestMessage(unittest.TestCase):
    def test_create_simple(self):
        msg = Message(role="user", content="hello")
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "hello")


class TestHistory(unittest.TestCase):
    def test_append_and_len(self):
        h = History("test")
        self.assertEqual(len(h), 0)
        h.append(Message(role="user", content="hi"))
        self.assertEqual(len(h), 1)

    def test_search(self):
        h = History()
        h.append(Message(role="user", content="find revenue tables"))
        h.append(Message(role="assistant", content="I found 3 tables."))
        h.append(Message(role="user", content="show me the schema"))
        self.assertEqual(len(h.search("tables")), 2)

    def test_search_case_insensitive(self):
        h = History()
        h.append(Message(role="user", content="Hello World"))
        self.assertEqual(len(h.search("hello")), 1)

    def test_as_text(self):
        h = History()
        h.append(Message(role="user", content="ping"))
        h.append(Message(role="assistant", content="pong"))
        text = h.as_text()
        self.assertIn("[user] ping", text)
        self.assertIn("[assistant] pong", text)

    def test_get_context_window_returns_all(self):
        h = History()
        for i in range(5):
            h.append(Message(role="user", content=f"msg {i}"))
        self.assertEqual(len(h.get_context_window()), 5)


class TestConnection(unittest.TestCase):
    def test_send_receive(self):
        async def _t():
            conn = Connection("test")
            await conn.inject_user_message("hello")
            msg = await conn.receive()
            self.assertEqual(msg.content, "hello")
            self.assertEqual(msg.role, "user")

        _run(_t())

    def test_agent_response(self):
        async def _t():
            conn = Connection("test")
            await conn.send("response text")
            self.assertEqual(await conn.read_agent_response(), "response text")

        _run(_t())


class TestMemory(unittest.TestCase):
    def test_set_get(self):
        async def _t():
            m = Memory("test")
            await m.set("k1", "v1")
            self.assertEqual(await m.get("k1"), "v1")

        _run(_t())

    def test_get_missing(self):
        _run(self._check())

    async def _check(self):
        m = Memory("test")
        self.assertIsNone(await m.get("no"))

    def test_peek_sync_read(self):
        async def _t():
            m = Memory("test")
            await m.set("k", "v")
            self.assertEqual(m.peek("k"), "v")
            self.assertIsNone(m.peek("missing"))

        _run(_t())

    def test_delete(self):
        async def _t():
            m = Memory("test")
            await m.set("k", "v")
            await m.delete("k")
            self.assertIsNone(await m.get("k"))

        _run(_t())

    def test_list_keys(self):
        async def _t():
            m = Memory("test")
            await m.set("foo_1", "a")
            await m.set("foo_2", "b")
            await m.set("bar_1", "c")
            self.assertEqual(sorted(await m.list_keys("foo")), ["foo_1", "foo_2"])

        _run(_t())

    def test_search(self):
        async def _t():
            m = Memory("test")
            await m.set("k1", "revenue data")
            await m.set("k2", "cost data")
            await m.set("k3", "revenue forecast")
            self.assertEqual(len(await m.search("revenue")), 2)

        _run(_t())


class TestCredentials(unittest.TestCase):
    def test_attenuate_subset(self):
        c = Credentials(token="t", scopes={"sql:read", "sql:write", "files:read"})
        n = c.attenuate({"sql:read"})
        self.assertEqual(n.scopes, {"sql:read"})

    def test_attenuate_rejects_superset(self):
        c = Credentials(token="t", scopes={"sql:read"})
        with self.assertRaises(ValueError):
            c.attenuate({"sql:read", "sql:write"})

    def test_attenuate_empty(self):
        c = Credentials(token="t", scopes={"sql:read"})
        self.assertEqual(c.attenuate(set()).scopes, set())


class TestAgentDef(unittest.TestCase):
    def test_default(self):
        ad = AgentDef()
        self.assertEqual(ad.tools, {})
        self.assertTrue(ad.async_enabled)
        self.assertTrue(ad.cancellable)
        self.assertFalse(ad.runtime)

    def test_with_values(self):
        ad = AgentDef(
            name="t",
            async_enabled=False,
            cancellable=False,
            runtime=True,
            memories={"p": MemoryConfig(scope="per_user")},
        )
        self.assertFalse(ad.async_enabled)
        self.assertFalse(ad.cancellable)
        self.assertTrue(ad.runtime)
        self.assertIn("p", ad.memories)


if __name__ == "__main__":
    unittest.main()
