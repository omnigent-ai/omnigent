"""Full functional test of the cognee integration against the REAL cognee.

Phase A drives omnigent.runtime.memory directly (real embedded store, real
LLM cognify). Phase B drives the same flow through the runner dispatch
boundary (_execute_cognee_tool). Prints PASS/FAIL per check + latencies.

Not part of the pytest suite: it needs the optional extra and a live LLM
key, and it spends real tokens. Run it manually::

    uv sync --extra cognee --group dev
    OPENAI_API_KEY=sk-... uv run --no-sync \
        python dev/cognee_functional_test.py /tmp/cognee-e2e-store

Use a FRESH store path per run — checks assume an empty store. Known flake
this harness exists to catch: a cognify racing its preceding add can
complete as a no-op, leaving the memory unindexed (the cross-agent recall
check then fails; see the cognify outcome log lines).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def timed(label: str, fn):
    t0 = time.monotonic()
    out = fn()
    dt = time.monotonic() - t0
    print(f"  ({label}: {dt:.2f}s)", flush=True)
    return out, dt


def main() -> int:
    from omnigent.runtime import memory as m

    data_root = Path(sys.argv[1])
    settings = {
        "data_root": str(data_root),
        "llm_api_key": os.environ["OPENAI_API_KEY"],
    }

    print(f"=== Phase A: memory boundary, store at {data_root} ===", flush=True)
    check("gate open (installed + no kill-switch)", m.cognee_available())

    # A1: remember into agent A's private dataset.
    fact = "Vasilije's favorite analytics database is DuckDB, used for churn analysis."
    stored, _ = timed(
        "add", lambda: m.memory_add(fact, "ag_e2e_alpha", settings=settings, node_set=["conv_e2e"])
    )
    check("add returns True", stored is True)

    # Drain the single-worker executor so cognify completed (FIFO barrier).
    print("waiting for background cognify...", flush=True)
    t0 = time.monotonic()
    m._get_background_executor().submit(lambda: None).result(timeout=900)
    print(f"  (cognify barrier: {time.monotonic() - t0:.2f}s)", flush=True)

    # A2: recall from own dataset — CHUNKS (raw, no LLM) and GRAPH_COMPLETION.
    chunks, _ = timed(
        "search CHUNKS",
        lambda: m.memory_search(
            "favorite analytics database",
            ["ag_e2e_alpha"],
            settings={**settings, "search_type": "CHUNKS"},
        ),
    )
    check(
        "CHUNKS recall finds the fact",
        any("DuckDB" in r for r in chunks),
        f"{len(chunks)} results",
    )
    gc, _ = timed(
        "search GRAPH_COMPLETION",
        lambda: m.memory_search(
            "What is Vasilije's favorite analytics database?", ["ag_e2e_alpha"], settings=settings
        ),
    )
    check("GRAPH_COMPLETION answers", any("uck" in r for r in gc), f"got: {gc[:1]}")

    # A3: isolation — a different agent's dataset must not see the fact.
    other = m.memory_search(
        "favorite analytics database",
        ["ag_e2e_beta"],
        settings={**settings, "search_type": "CHUNKS"},
    )
    check(
        "isolation: beta dataset sees nothing",
        not any("DuckDB" in r for r in other),
        f"{len(other)} results",
    )

    # A4: shared exchange — alpha publishes, beta reads the shared pool.
    stored2, _ = timed(
        "add shared",
        lambda: m.memory_add(
            "The release is planned for Friday.", "team_e2e_pool", settings=settings
        ),
    )
    check("shared add returns True", stored2 is True)
    m._get_background_executor().submit(lambda: None).result(timeout=900)
    shared = m.memory_search(
        "release planned", ["team_e2e_pool"], settings={**settings, "search_type": "CHUNKS"}
    )
    check(
        "exchange: shared pool recalls publication",
        any("Friday" in r for r in shared),
        f"{len(shared)} results",
    )

    # A5: repeated calls on fresh event loops (loop-binding regression probe).
    ok_repeat = True
    for _ in range(3):
        r = m.memory_search(
            "favorite analytics database",
            ["ag_e2e_alpha"],
            settings={**settings, "search_type": "CHUNKS"},
        )
        if not any("DuckDB" in x for x in r):
            ok_repeat = False
    check("3 sequential searches on fresh loops", ok_repeat)
    check("breaker still closed", m.breaker.allow())

    print("=== Phase B: runner dispatch boundary ===", flush=True)
    from omnigent.runner.tool_dispatch import _execute_cognee_tool

    def spec(config: dict[str, str], *names: str) -> SimpleNamespace:
        return SimpleNamespace(
            tools=SimpleNamespace(builtins=[SimpleNamespace(name=n, config=config) for n in names])
        )

    # Patch settings source so the dispatch path uses our temp store.
    m.cognee_settings = lambda: dict(settings)  # type: ignore[assignment]

    remember_spec = spec({"shared_dataset": "team_e2e_pool"}, "cognee_remember", "cognee_search")
    out = asyncio.run(
        _execute_cognee_tool(
            {"content": "Omnigent runners restart nightly at 03:00 UTC.", "scope": "shared"},
            tool_name="cognee_remember",
            agent_spec=remember_spec,
            conversation_id="conv_dispatch",
            agent_id="ag_dispatch_a",
        )
    )
    check("dispatch remember (shared)", "team_e2e_pool" in out, out)
    m._get_background_executor().submit(lambda: None).result(timeout=900)

    reader_spec = spec(
        {"shared_dataset": "team_e2e_pool", "search_type": "CHUNKS"}, "cognee_search"
    )
    out2 = asyncio.run(
        _execute_cognee_tool(
            {"query": "when do runners restart", "scope": "shared"},
            tool_name="cognee_search",
            agent_spec=reader_spec,
            conversation_id="conv_dispatch2",
            agent_id="ag_dispatch_b",
        )
    )
    check("dispatch cross-agent recall", "03:00" in out2, out2[:120])

    # Denial: reading another agent's private dataset without a grant.
    out3 = asyncio.run(
        _execute_cognee_tool(
            {"query": "anything", "dataset": "ag_e2e_alpha"},
            tool_name="cognee_search",
            agent_spec=spec({}, "cognee_search"),
            agent_id="ag_dispatch_b",
        )
    )
    check("dispatch denies ungranted dataset", "not granted" in out3, out3[:100])

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed ===", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
