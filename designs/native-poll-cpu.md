# Native-harness idle poll CPU

Design for the two load-proportional poll loops that dominate runner CPU on a
host running many concurrent native sessions:

- **#2702** — `TerminalInstance._idle_watch_loop_threaded`: one daemon thread
  per live terminal, fork+exec'ing tmux twice per tick.
- **#3000** — `forward_claude_transcript_to_session`: one asyncio task per live
  session, re-reading the whole bridge state on every tick.

Acceptance bar: **an idle session must cost near zero.** Both costs scale with
live terminals / live sessions, so they multiply exactly under sub-agent
fan-out.

Out of scope, deliberately: #2703 (YAML re-parse), #1349 (idle native session
reaping), #2421 (ownerless tree leak). See "Interaction with adjacent issues".

## 1. Pre-change mechanisms

### 1.1 Terminal idle watcher (#2702)

`TerminalInstance.start_idle_watcher_thread` spawns one daemon thread named
`terminal-idle-<name>-<key>`. Each tick in
`TerminalInstance._idle_watch_loop_threaded`:

1. `stop_event.wait(interval)` — the poll sleep.
2. `_capture_pane_for_idle_or_none()` → `subprocess.run(tmux … capture-pane)`.
3. `_pane_is_dead()` → `subprocess.run(tmux … list-panes -F '#{pane_dead} #{pane_dead_status}')`, then records the exit status with `_remember_exit_status`.
4. `_IdleDetector.tick(snapshot)` — pure diff/marker state machine.
5. Fires `on_activity` / `on_idle` / `on_exit`.

Two `subprocess.run` calls per tick. `_tmux_base_cmd()` uses the bare string
`"tmux"`, so `execvp` walks `PATH` — the issue's `strace` shows **4 `execve`
per invocation, 3 of them `ENOENT`**.

The base watcher already records `pane_dead_status`; the folded probe must
preserve that contract rather than adding it as new behaviour.

Two watcher configurations are wired by
`SessionResourceRegistry._start_terminal_activity_watcher`:

| watcher | interval | idle threshold | callbacks |
|---|---|---|---|
| generic terminal (no PTY-status-owning role) | 1.0 s | 10.0 s | `on_activity`, `on_exit` |
| the 7 PTY-only native agent terminals | 0.2 s | 1.0 s | `on_activity`, `on_idle`, `on_exit` |
| claude-native agent terminal | 0.2 s | 1.0 s | `on_activity`, `on_idle`, `on_exit`, `on_tick` (status-file poller) |

The asyncio sibling `_idle_watch_loop` has **no production caller** (only
`close()` stops it). It is left alone apart from inheriting the resolved tmux
path.

#### Latency consumers

| consumer | source | budget |
|---|---|---|
| `session.terminal.activity` pulse (web activity badge) | `_on_activity` → `activity_publisher` | web keeps the badge lit for 1.5 s (`ACTIVE_OUTPUT_WINDOW_MS`); already throttled to 1 emit/s by `_TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS` |
| session `running` status (Agents panel) | active status-file poller for claude-native; otherwise `_on_activity` → `status_publisher` | human-visible; sub-second desirable at turn start |
| session `idle` status | active status-file poller for claude-native; otherwise `_on_idle` → `status_publisher` | also memoised for exit classification (`_set_session_status_memo`) |
| terminal exit lifecycle | `_on_exit` → `_handle_terminal_exit` | seconds-scale is fine; it publishes a lifecycle event and cleans up |

The critical observation: **the `running` edge that users actually notice is
turn start, and turn start is initiated by the runner itself.** Native
harnesses inject from their harness process rather than through `send()`, but
the runner still knows when dispatch begins and can wake a backed-off watcher.

### 1.2 Claude transcript forwarder (#3000)

`forward_claude_transcript_to_session`
polls every `_DEFAULT_POLL_INTERVAL_S = 0.25` and, per tick, re-reads the bridge
state from scratch and runs ~10 independent scans. Measured locally with
cProfile on 4 idle sessions at fan-out 6: **~20 `open()` and ~17
`asyncio.to_thread` dispatches per session per tick**. The thread-pool
dispatches (future creation, context copy, lock traffic) are as expensive as
the I/O.

Everything the loop consumes is a file:

| file | writer | shape |
|---|---|---|
| `bridge.json` | bridge setup | atomic replace |
| `state.json` | hooks (`record_hook_event`) | atomic replace |
| `hooks.jsonl` | hooks | append-only |
| `context.json` | statusLine wrapper | atomic replace |
| `message_deltas.jsonl` | message-display hook | append-only |
| `<transcript>.jsonl` | Claude Code | append-only |
| `<session>/subagents/agent-*.jsonl` + `.meta.json` | Claude Code | append/create |

Plus the forwarder's own cursors (`*_forwarder.json`), which only change when
the forwarder did work.

#### Latency consumers

| consumer | budget |
|---|---|
| assistant-text deltas → live streaming in the web transcript | **tightest**: this is what makes streaming feel live. Must stay at the current 0.25 s. |
| transcript items, tool calls/results | same tick as deltas |
| turn-start `running` / turn-end `idle` status edges | sub-second |
| session cost + model mirror (feeds cost-budget policy gating) | sub-second within a turn |
| sub-agent status | 5 s quiescence heuristic already |

Because streaming latency is non-negotiable, **the poll interval cannot back
off.** The tick body has to get cheap instead.

## 2. Strategies considered

### #2702

| strategy | verdict |
|---|---|
| **A. tmux control mode (`tmux -CC`) `%output` notifications** | Rejected. Each terminal has its *own* tmux server, so this is one persistent control client process **per terminal** plus a reader thread. On the reported host that is ~42 new processes — the tax moved, not removed. Also gives raw output bytes, not rendered pane content, so the marker track (`_IDLE_MARKER_SUBSTRINGS`) would need a separate renderer. |
| **B. `pipe-pane -o 'cat >> file'`** | Rejected for the same reason: tmux spawns a shell per pane. Also raw bytes, not rendered cells. |
| **C. `/proc/<pane_pid>/stat` CPU-time pre-filter** | Rejected. Linux-only (the repo runs on macOS too), needs the pane's process *tree* (the agent is a grandchild of `pane_pid`), and walking `/proc` per tick is its own cost. |
| **D. Cheaper ticks + adaptive backoff** | **Chosen.** Three independent, composable reductions, all portable: resolve the tmux binary once (4 `execve` → 1); merge the two tmux invocations into one command sequence (2 fork+exec → 1); back the interval off after sustained quiescence, with an explicit wake. |

Strategy D's three parts multiply when backoff is allowed:
`8 execve/tick → 1 execve/tick`, then `5 ticks/s → 0.5 ticks/s` for a watcher
using the 0.2 s base. An active claude-native status-file poller deliberately
disables the interval reduction, because it needs every watcher tick.

### #3000

| strategy | verdict |
|---|---|
| **A. Interval backoff (0.25 → 1 → 5 s)** | Rejected as the primary fix. It buys idle CPU by directly spending streaming latency — the one budget that cannot move. |
| **B. Tear the forwarder down on `Stop`** | Rejected. `Stop` is not terminal: the user resumes the same session in the same pane, and the tail must be live when they do. This is the issue's own open question; the chosen fix makes it moot (a stopped harness writes nothing, so it costs nothing) without gambling on lifecycle semantics. It also stays clear of #1349's reaping decision. |
| **C. Memoise the individual file reads** | Partial. Kills the `_read_json_file` / `pathlib` share but leaves the ~17 `to_thread` dispatches and the ~10 scans. |
| **D. Gate the whole tick body behind a cheap change-detector** | **Chosen.** Keep ticking at 0.25 s — *zero* added latency — but make a no-change tick cost a handful of `stat`s instead of the full body. |

## 3. Chosen design

### 3.1 One mechanism or two?

**Two.** They share a diagnosis (unconditional per-unit polling) but not a
remedy, because their per-tick work has opposite shapes:

- The forwarder's inputs are **files**, so "did anything change?" is answerable
  for ~1% of the cost of the tick itself. Gate the work, keep the rate.
- The terminal watcher's input is **rendered tmux pane state**, which has no
  cheap out-of-band change signal short of spawning a per-pane process. There
  is nothing to gate on, so the rate has to move.

Forcing one abstraction over both would mean either paying a process per pane
(to give the terminal a file-like signal) or spending streaming latency (to give
the forwarder a backoff). Both are worse than two small, local fixes.

### 3.2 #2702 — cheaper ticks + post-idle backoff

**(a) Resolve the tmux binary once.** Module-level `functools.lru_cache` over
`shutil.which("tmux")`, falling back to the bare name so the failure message is
unchanged when tmux is absent. Cuts `execve` 4 → 1 per invocation. No behaviour
change.

**(b) Merge the pane-dead probe into the capture.** One tmux command sequence:

```
tmux -S <sock> -f /dev/null list-panes -t main -F '#{pane_dead} #{pane_dead_status}' \; capture-pane -t main -p -e
```

The first line carries liveness and the dead pane's exit status; the remaining
output is the pane capture. `list-panes` is intentional: unlike
`display-message`, it errors for an unknown target instead of silently falling
back to another pane. The folded method records `pane_dead_status` for
`TerminalExitEvent.exit_status`. It halves fork+exec per tick and removes the
race where the pane died between separate capture and probe calls.

**(c) Post-idle interval backoff.** Per tick:

```
changed        → interval = base                     (activity: full rate)
quiescent ≥ idle_threshold → interval = min(interval × 2, max_interval)
otherwise      → interval unchanged
```

`max_interval = min(base × 10, 5.0)` → 2.0 s for the claude-native watcher
(base 0.2), 5.0 s for generic terminals (base 1.0).

The runner supplies a dynamic backoff predicate. While claude-native's
`SessionStatusPoller` is active, the predicate is false and the watcher stays
at its 0.2 s base interval: that file is the sole status publisher, and its
`waiting` transitions and reconnect resyncs must not wait up to 2 s. Before
the file is found, or after the poller retires, the pane fallback may back off.
Other terminal roles have no status-file poller and retain adaptive backoff.

**The false-idle invariant.** Backoff only grows once `_IdleDetector` reports
`idle_notified` — i.e. strictly *after* the edge has fired. It is read off the
detector rather than re-timed in the loop on purpose: the loop's clock starts
when the watcher starts and the detector's when it takes its first snapshot, so
a loop-side quiescence timer runs one tick ahead and lets the interval grow
*before* the edge it is supposed to trail, delaying that edge. Consequences:

- Backoff can never make the idle edge fire **early**: `_IdleDetector` compares
  wall-clock (`time.monotonic()`) against the threshold, not tick counts, so
  sampling less often can only delay a decision, never advance it.
- Backoff can never make idle fire **when it shouldn't**: a session that is
  working repaints its pane, every tick sees a change, the interval stays at
  base, and the growth branch is never reached.
- What backoff *can* cost is a late `running` edge — the idle→running
  transition, bounded by `max_interval`.

**The wake path** collapses that cost for every case a user actually notices.
A per-watcher `_WakeSignal` is raised by:

- `_publish_turn_status(conv, "running")` in the runner, via
  `SessionResourceRegistry.wake_session_terminal_watchers` — **the turn-start
  hook that matters.** Native harnesses do *not* reach the pane through
  `TerminalInstance.send`: the executor calls `inject_user_message` from the
  harness process, which drives tmux over the socket directly, so the runner's
  watcher sees nothing in-process. `_publish_turn_status` is the one point
  every dispatch path passes through — background turns, continuation turns,
  the recovery path, and the streaming branch that never reaches
  `_run_turn_bg`. The wake runs *before* that function's native-harness
  suppression, because the harnesses whose status edge is terminal-driven are
  exactly the ones that need it.
- `TerminalInstance.send()` — the runner typing into a pane directly (tool-
  driven terminals).
- `TerminalInstance.note_client_interaction()` — attach/detach, focus, mouse,
  keystroke, resize from the web terminal.
- `_stop_idle_watcher_thread()` — so teardown does not wait out a backed-off
  sleep.

**Scope.** The turn-start wake fires only for
`PTY_STATUS_OWNING_TERMINAL_ROLES` — the eight harnesses whose pane watcher
keeps session status current. For claude-native an active status-file poller
riding that tick is the sole publisher; the pane is its fallback. A generic
shell or auxiliary pane drives only the
activity badge, and a session turn implies nothing about whether it will
produce output; waking it would put it on high-rate polling for an unrelated
turn, which is the cost this change exists to remove. The same frozenset gates
the watcher's own status emission, so the two cannot drift.

The wake and its reason live in one `_WakeSignal`, taken together under a
single lock by `consume()`. They began as a `threading.Event` beside a `bool`,
which meant a wake's two halves could be observed and cleared independently —
whether that was safe took an interleaving-by-interleaving argument, which is
the tell that the state wanted to be one object. Two properties are now
structural rather than argued: a wake can never be split across ticks, and one
raised after `consume()` returns stays pending for the next call instead of
being dropped.

**Grace.** A turn-start wake says output is *coming*, not that it has arrived,
and turn setup can outlast the idle threshold — so it also pins the base
interval for `_IDLE_POLL_WAKE_GRACE_SECONDS`. Two bounds keep that cheap:

- Only wakes that pass `expect_output=True` arm it. Client interactions do
  not: they are one-off repaints arriving per keystroke and mouse event, and
  arming a full window on each would hold the pane at base rate far longer
  than the repaint warrants.
- It is released the moment the pane actually changes. A normal turn therefore
  pays a handful of extra captures, not the whole window; the full window is
  spent only when the expected output never comes, which is exactly the case
  it exists for.

The grace remains necessary for the seven PTY-only status roles and for
claude-native before its status file resolves or after its poller retires. An
active claude-native poller already pins base cadence, so the grace is inert in
that state rather than being the source of responsiveness.

The sleep is split so a wake storm cannot become a fork storm:

```
phase 1: stop_event.wait(base_interval)          # mandatory, un-wakeable floor
phase 2: wake_signal.consume(interval - base)    # only extra backoff is skippable
```

Poll rate is therefore capped at `1/base` no matter how many wakes arrive — a
user dragging the mouse over an attached terminal cannot drive tmux faster than
today's rate. Phase 1 keeps the `close()` join window bounded by `base` exactly
as it is today. Teardown sets `stop_event` and separately raises `_WakeSignal`,
so phase 2 returns promptly while the stop condition remains independently
observable.

Residual (accepted, called out below): a pane parked on a permission prompt with
a blinking spinner changes every tick, so it never backs off. That session is
"idle" to a human but not to the pane, and treating it as backoff-eligible would
just oscillate (each backed-off sample sees a different spinner frame → reset).

### 3.3 #3000 — change-detector gate, unchanged interval

Per tick, before the body:

```
fingerprint = stat(each _WATCHED_BRIDGE_FILES) + stat(transcript)
            + stat(subagents_dir) + stat(each cached agent-*.jsonl)
              → {name: (st_mtime_ns, st_size, st_ino)}
```

Targets are pre-resolved once per session and the sub-agent listing is cached;
see §3.3.1 for why, and for what the first version got wrong.

`st_ino` is what makes this airtight: `_write_json_file` writes a temp file and
`os.replace`s it, so every state write lands a **new inode**. Append-only files
(`hooks.jsonl`, `message_deltas.jsonl`, transcripts) always grow `st_size`. A
same-size, same-nanosecond, same-inode rewrite is the only blind spot, and no
writer here can produce one.

Run the full body when **any** of:

1. the fingerprint changed (or this is the first tick — no baseline yet), **or**
2. fewer than `_IDLE_SETTLE_SECONDS = 8.0` have passed since the last change —
   the settle window that lets purely time-based transitions complete, **or**
3. a retry tracker or the cost-retry backoff has a post due, **or**
4. `_IDLE_RESYNC_SECONDS = 10.0` have passed since the last full body — a
   belt-and-braces resync that bounds the damage of any blind spot.

The settle window is sized against the in-process deadlines that fire with no
file change:

| deadline | value | covered by |
|---|---|---|
| sub-agent idle quiescence | 5.0 s | settle window |
| HTTP post retry | 1–30 s | explicit tracker check (3) |
| cost post retry | backoff | explicit `cost_retry_not_before` check (3) |

**Latency impact: none.** The poll interval is untouched, and any change to a
currently watched input file is seen on the very next 0.25 s tick. An input
that is not watched falls back to the resync — see §3.3.1 and the
"classification contract is not fail-closed" residual risk.

### 3.3.1 The detector must not watch its own outputs

The first version fingerprinted the bridge directory with `os.scandir` plus a
`DirEntry.stat` per entry, which is self-maintaining — a bridge file added
later is covered without anyone remembering. It also swept in the forwarder's
own cursor files, and that turned out to matter: writing a cursor moves the
fingerprint, which runs the next tick's body, which can write the cursor
again. On an idle session with a `message_deltas.jsonl` present the gate
engaged only **54 %** of ticks instead of settling.

So inputs are stat'ed by name from `_WATCHED_BRIDGE_FILES`. Two unwatched
buckets make the exclusion explicit: `_FORWARDER_OWNED_BRIDGE_FILES` contains
the loop's own cursors plus the shared dead-letter sink, while
`_OTHER_PRODUCER_BRIDGE_FILES` contains five adjacent-router outputs the loop
does not consume (`turn_router.json`, `turn_routing_done`,
`turn_replay_pending.json`, `turn_routing.log`, and `subagent_router.json`).

That trades the scan's self-maintaining property for an explicit contract,
guarded by four tests that fail for different mistakes:

- `test_fingerprint_classifies_every_declared_bridge_file` sweeps the `*_FILE`
  constants of the seven modules that write into a bridge dir — the bridge,
  forwarder, statusLine wrapper, message-display hook, shared post-delivery
  module, turn router, and sub-agent router. Blind to a producer nobody listed.
- `test_fingerprint_classifies_every_file_a_live_session_writes` drives those
  producers and requires every file left in the directory to be classified.
  Blind to a producer or branch the scenario does not reach.
- `test_bridge_file_disposition_controls_fingerprint` replaces each known
  producer file and pins whether the observed fingerprint must move. Its
  semantic table is independent of the production tuples, so moving a watched
  input into an unwatched bucket fails on behaviour rather than spelling.
- `test_other_producer_bridge_files_are_not_opened_through_path_open` seeds all five
  adjacent-router outputs and observes real full ticks, requiring that none is
  opened through `Path.open`.

**None is fail-closed, and together they are not either** — they are four
partial nets over the same contract, and the residual is stated in §8. A
genuinely closed version would need every producer to take its filenames from
one registry, but nothing forces a producer to use a registry rather than a
literal, so that buys discoverability rather than a guarantee — at the cost of
threading a new dependency through seven modules. Worth revisiting if the
producer set grows; not worth it for the current seven.

A miss costs bounded staleness rather than permanent loss: an unwatched
input's changes surface on the `_IDLE_RESYNC_SECONDS` sweep instead of the
next tick. That is eventual observation, not correctness during the gap — see the
"classification contract is not fail-closed" residual risk.

Paths are pre-resolved once per session into `_BridgeInputPaths` and held as
plain strings. This is not incidental: a by-name version that built
`bridge_dir / name` per tick measured *slower* than the directory scan it
replaced, because `pathlib` construction costs more than the `os.stat` it
feeds.

Sub-agent membership is cached and re-listed when the `subagents/` directory's
own mtime moves, **or while that mtime is too recent to trust**. Appends to an
existing transcript move neither the directory mtime nor the member list,
which is why the cached transcripts are still stat'ed individually every tick.

The second condition is not belt-and-braces. `st_mtime_ns` reports nanoseconds
but filesystems do not store them: Linux stamps directory mtimes from the jiffy
clock — 4 ms at the common `CONFIG_HZ=250` — and older filesystems round to
1-2 s. An entry created inside the tick already recorded leaves the mtime
unchanged, and nothing moves it afterwards either, so comparing mtimes alone
loses that sub-agent until the resync. Measured on a `CONFIG_HZ=250` box:
adding an entry left the directory mtime unchanged **194 times out of 200**.
APFS stamps at ~50 µs and misses 0/200, which is why this survived review and
only surfaced on an integration build.

`_DIR_MTIME_RACY_WINDOW_NS` (2 s, comfortably past the 1-2 s filesystems)
therefore forces a re-list while the mtime sits within that window of now —
the same "racily clean" problem git solves for its index.

The window alone is not enough, because **a coarse mtime records the start of
its bucket, not the moment of the change**. An entry landing late in a 2 s
bucket can already read as older than the window by the time the next tick
runs; with the directory key unchanged and the mtime no longer recent, nothing
would re-list — and nothing ever moves that key again. So a listing taken while
the mtime was racy is marked *provisional*, and that uncertainty is latched
until one further listing is taken with the mtime settled.

The age check is one-sided rather than a distance. A stamp from the future is
untrustworthy however far ahead it is, and `abs()` would call one 5 s ahead
"settled" simply because it is far from now — exactly inverting the intent for
a clock-skewed network filesystem.

Cost: extra listings only just after a change, plus one when the window
expires, and nothing once the directory is quiet — so the steady state this
gate exists to make cheap is unaffected, measured unchanged at fan-out 6
and 100.

### 3.4 Blast radius, kill switches, observability

| knob | default | effect when disabled |
|---|---|---|
| `OMNIGENT_TERMINAL_IDLE_POLL_BACKOFF=0` | enabled | watcher polls at the fixed base interval, exactly as today |
| `OMNIGENT_CLAUDE_FORWARDER_IDLE_GATE=0` | enabled | forwarder runs the full body every tick, exactly as today |

Both are read once per loop start, so a restart picks up a change. Neither
switch touches the tmux-path resolution or the merged probe — those are pure
cost reductions with no behavioural surface, so they carry no switch.

Field diagnosability, at `DEBUG`, one line per transition rather than per tick:

| where | logged when |
|---|---|
| `_idle_watch_loop_threaded` | the interval grows a step (`pane quiescent; poll a -> b`) |
| `_idle_watch_loop_threaded` | a pane change collapses it back to base (`pane changed`) |
| `_idle_watch_loop_threaded` | a wake collapses it back to base (`idle watcher woken`) |
| `forward_claude_transcript_to_session` | the gate first starts skipping (`idle; skipping unchanged polls`) |
| `forward_claude_transcript_to_session` | the gate re-opens (`resumed`) |

A regression therefore shows up as either "never engages" (no CPU win) or
"engaged while a turn was live" (a bug), both visible in the runner log without
a rebuild.

### 3.5 Platform reach

Everything used is portable: `shutil.which`, `subprocess.run`,
`threading.Event` / `threading.Condition`, `os.scandir`, `os.stat`. No
`inotify`, no `kqueue`, no `/proc`. There is no degraded path to fall back to
because there is no platform-specific path.

`st_ino` and `st_mtime_ns` are meaningful on both APFS and ext4, but their
*resolution* is not portable and must not be assumed — see the granularity
discussion in §3.3.1. Timestamp-derived logic is written against a window, not
against equality, precisely because a nanosecond field can carry a 4 ms (or
1 s) value.

## 4. Measurement

`dev/benchmarks/native_poll_cpu.py`, two scenarios, both holding everything
genuinely idle (no turns, no pane output, no transcript growth):

Below is the schedule each Set 1 row was actually produced by. Run from the
candidate root. **Three separate campaigns** produced the rows, each with its
own base worktree materialised **before** its measurements, and each is given
here as its own block because their round structures differ. One scenario per
invocation throughout.

Read the loop bodies as the definition of what "paired" means in Set 1: two
readings are a **matched pair** for a round if, and only if, **both** hold —

1. they were produced by invocations in the **same iteration of the same loop**
   below (the same round, on the same host, with no other round intervening);
   and
2. one of them is that iteration's **base** (`d720d2762`) reading and the other
   is the **branch** reading being compared against it.

Clause 2 is what keeps the pairing base-versus-branch, which is the only
comparison Set 1 models: two branch readings from one iteration are never a
pair, however close together they ran.

Adjacency inside the iteration is *not* required, and one iteration can yield
more than one pair: the `rev` loop runs base, backoff and pinned in a single
iteration, so that round supplies a base/backoff pair **and** a base/pinned
pair — even though backoff sits between the latter two — while its
backoff/pinned combination is **not** a pair, because neither member is the
base reading. The other exclusion is the c1 pinned runs: they occupy a loop of
their own whose iterations contain no base invocation, so clause 2 can never be
satisfied there and they pair with nothing. That is exactly why Set 1 never
treats them as matched.

```bash
# Shared preamble. The conditional kwarg splat keeps the script compatible with
# the base watcher's older signature; the base watcher has no backoff predicate
# at all, so one base terminal row is the before-side for BOTH branch predicate
# states.
benchmark_source="$PWD/dev/benchmarks/native_poll_cpu.py"
bench="uv run python -m dev.benchmarks.native_poll_cpu"

materialise_base() {  # $1 = target dir
  git worktree add --detach "$1" d720d2762
  cp "$benchmark_source" "$1/dev/benchmarks/native_poll_cpu.py"
}

# ---------------------------------------------------------------------------
# CAMPAIGN c1 (author) — terminal rounds c1 r1-r3, the author pinned block,
# and ALL forwarder rows.
# ---------------------------------------------------------------------------
c1_base="$(mktemp -d /tmp/omnigent-native-poll-base.XXXXXX)"
materialise_base "$c1_base"

# c1 step 1 — terminal base/backoff MATCHED PAIRS: both in one iteration.
for run in 1 2 3; do
  (cd "$c1_base" && $bench terminal --terminals 8 --seconds 30 --warmup 6)
  $bench terminal --terminals 8 --seconds 30 --warmup 6
done

# c1 step 2 — the author's --pin-base runs were NOT interleaved with base.
#   They ran as this separate block afterwards, reusing step 1's base rows as
#   their before-side. These three pinned rows are therefore NOT matched pairs
#   and Set 1 never treats them as such.
for run in 1 2 3; do
  $bench terminal --terminals 8 --seconds 30 --warmup 6 --pin-base
done

# c1 step 3 — forwarder MATCHED PAIRS, base then current in one iteration.
for run in 1 2 3; do
  (cd "$c1_base" && $bench forwarder --sessions 8 --seconds 30 --warmup 15 --fanout 0)
  $bench forwarder --sessions 8 --seconds 30 --warmup 15 --fanout 0
done
for run in 1 2 3; do
  (cd "$c1_base" && $bench forwarder --sessions 8 --seconds 30 --warmup 15 --fanout 6)
  $bench forwarder --sessions 8 --seconds 30 --warmup 15 --fanout 6
done
for run in 1 2 3; do
  (cd "$c1_base" && $bench forwarder --sessions 4 --seconds 30 --warmup 15 --fanout 100)
  $bench forwarder --sessions 4 --seconds 30 --warmup 15 --fanout 100
done

git worktree remove --force "$c1_base"

# ---------------------------------------------------------------------------
# CAMPAIGN c2 (author) — terminal rounds c2 r4-r8. Base worktree
# re-materialised from the same commit. No pinned runs in this campaign, which
# is why five base rows here have no pinned counterpart.
# ---------------------------------------------------------------------------
c2_base="$(mktemp -d /tmp/omnigent-native-poll-base.XXXXXX)"
materialise_base "$c2_base"

# c2 — terminal base/backoff MATCHED PAIRS.
for run in 4 5 6 7 8; do
  (cd "$c2_base" && $bench terminal --terminals 8 --seconds 30 --warmup 6)
  $bench terminal --terminals 8 --seconds 30 --warmup 6
done

git worktree remove --force "$c2_base"

# ---------------------------------------------------------------------------
# CAMPAIGN rev (independent reviewer) — terminal rounds rev r1-r3, from an
# independently materialised base worktree. This campaign ran all THREE shapes
# inside each round, which is what makes its pinned rows matched pairs. It is
# NOT the c1 step-1/step-2 pattern.
# ---------------------------------------------------------------------------
rev_base="$(mktemp -d /tmp/omnigent-native-poll-base.XXXXXX)"
materialise_base "$rev_base"

# rev — all three shapes run inside ONE iteration, so this round yields exactly
#   TWO MATCHED PAIRS, both against its base reading: base/backoff and
#   base/pinned. Pinned need not sit next to base to be matched, only in the
#   same iteration. backoff/pinned is NOT a pair — neither member is base.
#   The three true pinned pairs Set 1 relies on come from here and nowhere else.
for run in 1 2 3; do
  (cd "$rev_base" && $bench terminal --terminals 8 --seconds 30 --warmup 6)
  $bench terminal --terminals 8 --seconds 30 --warmup 6
  $bench terminal --terminals 8 --seconds 30 --warmup 6 --pin-base
done

git worktree remove --force "$rev_base"
```

The terminal scenario launches real tmux terminals and reports `RUSAGE_CHILDREN`
(the tmux processes) as well as `RUSAGE_SELF`, plus a tmux-invocation count. The
forwarder scenario runs real forwarder tasks against a local sink server and
reports `RUSAGE_SELF`.

Both scenarios take a `--warmup` that runs before the measurement window opens,
defaulting past each loop's settle period. Without it the window captures the
tail of the last activity burst instead of steady-state idle, which understates
the win by roughly 3x.

The forwarder scenario builds the full bridge directory a live session
accumulates (~9 files including `message_deltas.jsonl`). An earlier version
created only 4, which made every per-file cost look smaller than production
and hid the self-triggering gate in §3.3.1 entirely — the deltas path never
ran, so the cursor it churns was never written.

### Results

Three measurement sets follow, each with a different before-side. Read the
scope line before quoting any factor: only the first set compares the current
tree against base `d720d2762`.

#### What the terminal scenario actually configures

The terminal benchmark constructs a bare `TerminalInstance` and calls
`start_idle_watcher_thread` directly. There is **no** `SessionResourceRegistry`,
**no** `resource_role`, **no** session-status publisher, **no**
`SessionStatusPoller` and **no** `on_tick` on either side of any comparison
below. It passes `idle_threshold_s=1.0` and `poll_interval_s=0.2` — the cadence
`resource_registry.py` gives the eight `PTY_STATUS_OWNING_TERMINAL_ROLES`
terminals (`_CLAUDE_NATIVE_STATUS_IDLE_THRESHOLD_SECONDS` /
`_CLAUDE_NATIVE_STATUS_POLL_INTERVAL_SECONDS`).

A **generic** pane is a different shape and is *not* measured anywhere in this
section. For a generic pane `emit_status` is false, so the registry starts the
watcher with no interval arguments at all and it falls back to
`_IDLE_POLL_INTERVAL_SECONDS = 1.0` / `_IDLE_THRESHOLD_SECONDS = 10.0` — a 5x
slower base cadence, so a generic pane's *before* side is roughly 5x cheaper
than every "before" number here, and its factor cannot be read off these tables.

The two branch-side predicate states map to roles as follows:

- **backoff allowed** (no `--pin-base`) — the seven PTY-status-owning roles
  other than claude-native. They get `status_poller is None`, hence
  `idle_poll_backoff_allowed=None`, hence backoff always permitted. The
  benchmark reproduces that shape exactly.
- **base pinned** (`--pin-base`) — injects *only*
  `idle_poll_backoff_allowed=lambda: False`, i.e. claude-native while its status
  file owns the session. This reproduces the active poller's effect on
  **cadence**, not its per-tick cost: no `on_tick` runs on either side, so the
  `SessionStatusPoller.tick()` `stat` that claude-native performs every tick is
  absent from the "after" column. Read this row as the cadence floor for
  claude-native, not as a complete model of it.

#### Set 1 — current tree vs base `d720d2762`, one Linux host

Linux (24-core shared dev host, load ~1.3 at start of the first campaign),
Python 3.12.13, 30 s windows, `--warmup 6` (terminal) / `--warmup 15`
(forwarder), base materialised via `git worktree add --detach` per the repro
block above. "Campaign" below means one of the three labelled campaign blocks in
that repro block — its own base worktree, its own back-to-back rounds; it is not
a session count. **Three campaigns** contribute terminal rows (author c1, author
c2, reviewer rev); the forwarder rows come from campaign c1 only. Unit
counts are whatever each table's row says: the terminal rows are **8 bare
`TerminalInstance`s** with no application session at all, and the forwarder rows
are **8 synthetic forwarder sessions** (4 at fan-out 100). Every raw reading is
listed; no outlier was dropped.

**Scheduling, precisely.** Base and branch alternate inside each round for the
terminal backoff-allowed rows and for all three forwarder shapes, so drift hits
both sides of a pair and each such row yields a genuine paired ratio. The
**`--pin-base` rows are the exception**: the author's three ran as their own
block after the c1 base rows, not drift-paired against them, so only the
reviewer's three pinned runs — which ran base, backoff and pinned inside one
round — are matched pairs. Every pinned aggregate that spans all six pinned runs
is therefore a **marginal** statistic and is labelled as one; the only
matched-pair pinned figure is the n=3 reviewer row. Either way the invocation
count carries the claim.

The base tree has no backoff predicate and no backoff mechanism at all, so the
single set of base rows is the correct before-side for **both** branch predicate
states.

##### Terminal — every reading, presented once

All terminal readings live in this one place; there is no second, superseded
terminal table anywhere in §4. Three campaigns, same host, same invocation
(`--terminals 8 --seconds 30 --warmup 6`): the author's original three rounds
(**c1**), five further author rounds with the base worktree re-materialised from
the same commit (**c2**), and three independent rounds by a reviewer who
materialised their own base worktree (**rev**). Rounds `c1 r1`–`c1 r3` are the
original three that earlier revisions of this note reported on their own.

Base and backoff-allowed alternate within every round in all three campaigns, so
each row below is a genuine **matched pair** and its ratio is meaningful:

| round | base CPU % | backoff CPU % | paired CPU ratio | base inv/s | backoff inv/s | paired inv ratio |
|---|---:|---:|---:|---:|---:|---:|
| c1 r1 | 14.6892 | 0.9690 | 15.1591x | 8.46 | 0.50 | 16.92x |
| c1 r2 | 15.9013 | 0.9639 | 16.4968x | 8.35 | 0.50 | 16.70x |
| c1 r3 | 15.3303 | 0.9912 | 15.4664x | 8.35 | 0.50 | 16.70x |
| c2 r4 | 16.2989 | 0.8048 | 20.2521x | 8.28 | 0.50 | 16.56x |
| c2 r5 | 15.2219 | 0.9294 | 16.3782x | 8.37 | 0.50 | 16.74x |
| c2 r6 | 15.0571 | 0.9737 | 15.4638x | 8.41 | 0.50 | 16.82x |
| c2 r7 | 14.7716 | 1.0413 | 14.1857x | 8.47 | 0.50 | 16.94x |
| c2 r8 | 15.3576 | 0.8929 | 17.1997x | 8.40 | 0.50 | 16.80x |
| rev r1 | 16.6338 | **1.5786** | **10.5371x** | 8.25 | 0.50 | 16.50x |
| rev r2 | 14.7607 | 0.9427 | 15.6579x | 8.47 | 0.50 | 16.94x |
| rev r3 | 16.2601 | 0.9266 | 17.5481x | 8.25 | 0.50 | 16.50x |

The `--pin-base` runs are **not** all matched pairs and are kept separate for
that reason. The author's three ran as their own block after the c1 base rows
(see the scheduling note above); only the reviewer's three were interleaved with
a base run in the same round:

| run | pinned CPU % | pinned inv/s | schedule | round-mate base row | paired CPU ratio | paired inv ratio |
|---|---:|---:|---|---|---:|---:|
| c1 block 1 | 8.8927 | 4.53 | own block, after c1 base | none | — | — |
| c1 block 2 | 7.5721 | 4.60 | own block, after c1 base | none | — | — |
| c1 block 3 | 8.0698 | 4.58 | own block, after c1 base | none | — | — |
| rev r1 | 8.1762 | 4.56 | interleaved | 16.6338 % / 8.25 | 2.0344x | 1.8092x |
| rev r2 | 8.8012 | 4.50 | interleaved | 14.7607 % / 8.47 | 1.6771x | 1.8822x |
| rev r3 | 8.9868 | 4.51 | interleaved | 16.2601 % / 8.25 | 1.8093x | 1.8293x |

**Backoff-allowed — the paired result.** The 11 matched pairs are the statistic
this sampling design actually supports:

| paired statistic (n = 11) | min | median | max |
|---|---:|---:|---:|
| terminal CPU, base → backoff allowed | **10.54x** | **15.66x** | 20.25x |
| terminal tmux invocations, base → backoff allowed | **16.50x** | **16.74x** | 16.94x |

A deliberately conservative **unmatched-extrema** bound — the lowest base
reading over the highest backoff reading, `14.6892 / 1.5786 = 9.31x` — is also
quotable, but those two readings come from different campaigns and were never
taken together. It is a floor construction, **not an observed pairing**. The
observed paired minimum is 10.54x.

Marginal (unpaired) distributions, for completeness:

| pooled shape | n | min | median | max |
|---|---:|---:|---:|---:|
| base CPU | 11 | 14.6892 % | 15.3303 % | 16.6338 % |
| backoff-allowed CPU | 11 | 0.8048 % | 0.9639 % | **1.5786 %** |
| base tmux invocations | 11 | 8.25 /s | 8.37 /s | 8.47 /s |
| backoff-allowed tmux invocations | 11 | 0.50 /s | 0.50 /s | 0.50 /s |

**`--pin-base` — a marginal estimate, and labelled as one.** Only three of the
six pinned runs are round-paired with a base run, so no 6-pair statistic exists.
The first two figures below are **marginal**: they divide a base median by a
pinned median drawn from a *different* and partly *differently scheduled* set of
runs. **Three** variants, all shown because they differ; only the third is a
matched-pair statistic:

| pinned derivation | base rows used | n (base vs pinned) | CPU factor | invocation factor |
|---|---|---|---:|---:|
| **corresponding-campaign** (preferred) | c1 + rev only — the campaigns that actually contain the pinned runs | 6 vs 6 | 15.6158 / 8.4887 = **1.84x** | 8.35 / 4.545 = **1.84x** |
| all-base marginal | all 11, incl. 5 c2 rows with no pinned counterpart | 11 vs 6 | 15.3303 / 8.4887 = 1.81x | 8.37 / 4.545 = 1.84x |
| reviewer's 3 true pairs (the only matched pinned data) | rev only, matched | 3 pairs | median **1.81x** (1.68–2.03x) | median **1.83x** (1.81–1.88x) |

Use **1.84x**. The all-base variant mixes an 11-row base median against a 6-row
pinned median across two schedules and is shown only so the difference is
visible rather than inferred. Cross-extrema bounds are identical under both base
sets (CPU 1.63x–2.20x, invocations 1.79x–1.88x) because the 6-row subset
contains both the overall base minimum and maximum; only the median moves.

**Which figure is robust.** The **invocation rate is the robust one** and is the
figure to quote: 0.50 /s/terminal in **all 11** backoff-allowed runs across three
campaigns and two independent samplers, giving a paired range of 16.50–16.94x
that holds on every reading taken. Pinned invocations are equally tight,
4.50–4.60 /s, giving 1.79x–1.88x however the rows are grouped.

The **CPU factor is not robust enough for a point headline**. One reviewer
backoff-allowed sample landed at 1.5786 %, ~59 % above the highest of the
author's eight and the only reading above 1.05 % in eleven; that round's own
pair is 10.54x against a paired median of 15.66x. The direction is unaffected —
every one of the 11 pairs is a large win — but the honest CPU statement is
**"roughly 10–20x, observed paired minimum 10.5x"**, not a single multiplier.
The pinned CPU claim is likewise carried by invocations, not CPU.

No attempt was made to reconcile the 1.5786 % outlier away. This is a shared
host with other live agent sessions; the tail is real and a maintainer
re-running the block may land in it.

Halving processes per tick does not give exactly 2x fewer invocations because
the 0.2 s sleep is followed by blocking tmux work; removing one process shortens
that work and lets the folded watcher tick more often.

##### Forwarder

Same campaign as terminal rounds c1, base and branch alternating inside each
round:

| shape | run | base `d720d2762` | this branch |
|---|---:|---:|---:|
| fan-out 0, 8 sessions | 1 | 4.3343 % | 0.3640 % |
| fan-out 0, 8 sessions | 2 | 5.2218 % | 0.3829 % |
| fan-out 0, 8 sessions | 3 | 4.2855 % | 0.3247 % |
| fan-out 6, 8 sessions | 1 | 7.4383 % | 0.5780 % |
| fan-out 6, 8 sessions | 2 | 7.8422 % | 0.5569 % |
| fan-out 6, 8 sessions | 3 | 7.0324 % | 0.4652 % |
| fan-out 100, 4 sessions | 1 | 24.9971 % | 2.4478 % |
| fan-out 100, 4 sessions | 2 | 26.2428 % | 2.4293 % |
| fan-out 100, 4 sessions | 3 | 27.2336 % | 2.3091 % |

| comparison | before (median, range) | after (median, range) | median factor | worst-case bound |
|---|---|---|---:|---:|
| forwarder, fan-out 0 (8 sessions) | 4.3343 % (4.2855–5.2218) | 0.3640 % (0.3247–0.3829) | **11.9x** | **11.2x** |
| forwarder, fan-out 6 (8 sessions) | 7.4383 % (7.0324–7.8422) | 0.5569 % (0.4652–0.5780) | **13.4x** | **12.2x** |
| forwarder, fan-out 100 (4 sessions) | 26.2428 % (24.9971–27.2336) | 2.4293 % (2.3091–2.4478) | **≥10.8x** | **≥10.2x** |

The fan-out-100 base rows are a **lower bound**, not a measurement of demand.
All four forwarders share one event loop thread, and total process CPU
(`RUSAGE_SELF`, which also covers the sink server's threads) was 99.99 / 104.97
/ 108.93 % of a core across the three base runs. The loop was therefore at or
near saturation, so base per-session cost is at least what is shown and the
factor is at least what is shown. The branch rows are nowhere near that ceiling
(9.24–9.79 % of a core total), so they are unclipped.

These forwarder factors are much larger than the macOS set below because the
macOS "before" column was measured on the sparse 4-file bridge fixture, which
understates the per-file cost that dominates the base detector. Set 1 uses the
current ~9-file fixture on both sides.

#### Set 2 — earlier macOS run (historical; sparse fixture on the forwarder rows)

Pre-change source vs an **earlier build of this branch**, macOS 27 / M-series,
Python 3.12.13, 30 s window, everything idle. The branch-side vintage predates
the §3.3.1 detector fix; the build's `_scan_into_fingerprint` and
`_bridge_input_fingerprint` symbols are no longer present in the tree (§7.3.1).
This set is retained for the `execve`-per-invocation accounting (4 → 1), which
is a structural property of the change rather than a host measurement. It is
**not** a current-tree-vs-base comparison.

| scenario | before | after | factor |
|---|---|---|---|
| terminal, base tree (no predicate for any role) → backoff allowed (7 PTY-only native roles; **not** generic panes), 8 terminals | **6.17 %** of a core / terminal | **0.42 %** | **15x** |
| terminal, same base → backoff-allowed predicate states, tmux invocations | 8.57 /s / terminal | 0.50 /s | **17x** |
| terminal, same base → backoff-allowed predicate states, `execve` (4 per invocation → 1) | ~34 /s / terminal | 0.50 /s | **69x** |
| forwarder, no fan-out (8 sessions), **sparse 4-file fixture, earlier branch build** | **0.71 %** of a core / session | **0.12 %** | 6.1x |
| forwarder, fan-out 6 (8 sessions), **sparse 4-file fixture, earlier branch build** | **1.47 %** of a core / session | **0.15 %** | 9.9x |
| forwarder, fan-out 100 (4 sessions), **sparse 4-file fixture, earlier branch build** | **7.05 %** of a core / session | **0.65 %** | 10.9x |

**Precision caveat — these factors are not auditable from this table.** The
raw per-run readings behind Set 2 were not retained; only the two-decimal
aggregates above survive, and the factors were computed from the unrounded
readings. Every factor in the table was re-checked against its own displayed
operands. Three do not recompute from them:

| row | displayed operands | quotient at displayed precision | printed factor |
|---|---|---:|---:|
| forwarder, no fan-out | 0.71 / 0.12 | 5.9x | 6.1x |
| forwarder, fan-out 6 | 1.47 / 0.15 | 9.8x | 9.9x |
| forwarder, fan-out 100 | 7.05 / 0.65 | 10.8x | 10.9x |

All three are reachable from operands that round to the values shown (the
displayed pairs admit up to 6.22x, 10.17x and 10.94x respectively), so this is
missing precision rather than a demonstrated arithmetic error — but it cannot be
confirmed from what is retained. **Treat all three as approximate.** The other
three rows do recompute at their displayed precision: 6.17 / 0.42 = 14.7 → 15x,
8.57 / 0.50 = 17.1 → 17x, and 4 × 8.57 / 0.50 = 68.6 → 69x (the `execve` before
column is printed rounded as "~34").

The three forwarder factors here are also optimistic on both columns and are
superseded by Set 1, whose raw rows are all retained. The absolute CPU
percentages must not be compared across the Linux and macOS tables in either
direction: per-invocation tmux cost and per-`stat` cost differ materially
between the hosts.

An earlier Linux terminal measurement, taken on the same host under heavier
load, recorded base `d720d2762` at 19.3378 / 18.7224 / 23.2788 % and 7.97 /
8.00 / 7.44 invocations/s/terminal, against `--pin-base` at 14.8720 / 10.3462 /
10.1234 % and 4.11 / 4.44 / 4.48 /s. Those rows were superseded by the Set 1
re-run rather than averaged with it — the host was materially busier, which
raised CPU and *lowered* tick rate on both sides. The invocation-count factor
agreed: 7.97 / 4.44 = 1.8x then, 8.35 / 4.58 = 1.82x now.

Thus claude-native, whose active status poller keeps the predicate false, gets
the robust **~1.8x tmux-invocation** reduction from folding the probe and
caching the tmux path, but no adaptive-backoff reduction.

Extrapolating to the reported host (~20 sessions with fan-out), the forwarder
alone goes from ~29 % of a core to ~3 % — that is 20 × the Set 2 fan-out-6 row
(20 × 1.47 = 29.4, 20 × 0.15 = 3.0), so it inherits Set 2's sparse-fixture
caveat and its macOS per-`stat` cost. Terminal savings depend on role: an
active claude-native status poller keeps the 0.2 s cadence and receives only the
one-process/one-`execve` reductions, while the seven other PTY-status-owning
native roles also receive the measured interval reduction. Generic panes receive
the same structural reductions from a 5x cheaper starting cadence, and are not
measured here.

#### Set 3 — detector fix (§3.3.1), branch-vs-branch on the realistic fixture

**Deployed** code (an intermediate build of this branch, not base
`d720d2762`) vs this branch, 60 s window, 8 sessions (4 at fan-out 100),
everything idle. This set isolates the §3.3.1 detector change alone; for the
current-tree-vs-base forwarder numbers see Set 1.

| scenario | deployed | after | factor |
|---|---|---|---|
| forwarder, no fan-out | **0.47 %** of a core / session | **0.13 %** | 3.7x |
| forwarder, fan-out 6 | **0.79 %** of a core / session | **0.17 %** | **4.6x** |
| forwarder, fan-out 100 | **2.85 %** of a core / session | **0.65 %** | **4.4x** |
| gate engagement, no fan-out | 54 % of ticks | **97.5 %** | — |

**Precision caveat.** As with Set 2, the raw per-run readings were not retained
and the factors were computed from unrounded values. Every factor in this set
was re-checked against its displayed operands. One does not recompute:
0.47 / 0.13 = **3.6x** at displayed precision against a printed **3.7x**. It is
reachable from operands that round to those values (up to 3.80x), so treat it as
approximate. The other two do recompute: 0.79 / 0.17 = 4.65 → 4.6x and
2.85 / 0.65 = 4.38 → 4.4x. The gate-engagement row states no factor. For a
current-tree-vs-base forwarder number with raw rows retained, use Set 1.

Most of that is the gate finally settling rather than the detector being
cheaper; the fingerprint itself is ~2x cheaper at fan-out 0 (28.7 → 14.1 µs,
2.04x) and ~1.7x at fan-out 100 (252 → 149 µs, 1.69x — the "~2x" shorthand is
loose at that end). The remaining 2.5 % of ungated ticks is the 10 s resync, as
designed.

### High-fan-out budget

Forwarder CPU stays **proportional to accumulated fan-out**: every sub-agent
transcript a session has ever produced stays on disk and is stat'ed on every
tick. The sibling `agent-*.meta.json` files are exempt — read once at
discovery, never re-read — and the directory is re-listed only when its own
mtime moves, so the per-tick cost is one stat per transcript plus one for the
directory. That directory stat is in the fingerprint, which is what keeps a
new sub-agent's arrival visible even when its meta file lands first.

The remaining proportionality is inherent to a change-detector that must
notice growth in any of those files, and the shape is unchanged from before —
the constant is ~11x smaller. Budget to plan against: **~0.65 % of a core per
idle session at fan-out 100** on the macOS host of Set 2, and **~2.4 %** on the
Linux host of Set 1 — the ratio to fan-out 6 holds on both, but the absolute
figure is per-`stat` cost and does not port between hosts. Either way, a session
with 100 accumulated sub-agents costs four to five fan-out-6 sessions on the
same host (7.05 / 1.47 = 4.8 on macOS, 2.4293 / 0.5569 = 4.4 on Linux).
Removing that would require
knowing which sub-agents can no longer produce output, and the 5 s quiescence
heuristic that marks them idle is not that — a sub-agent inside a long tool
call reads idle and then resumes, so skipping it would strand its output.

The forwarder's residual at low fan-out is dominated by the fingerprint itself
(~59 µs/tick, ~66 % of what is left) plus the bare asyncio wake at 4 Hz. Both
are the irreducible cost of *not* backing the interval off, which is what keeps
streaming latency unchanged.

## 5. Interaction with adjacent issues

- **#2703** (YAML re-parse during fan-out) — disjoint code path
  (agent-bundle loading, not the poll loops). No overlap.
- **#1349** (idle native sessions never reaped) — **closed**. This change
  deliberately does *not* reap anything. It makes an un-reaped idle session
  cheap, which lowers the urgency of reaping but does not substitute for it: the
  memory a reaper reclaims is untouched here.
- **#2421** (ownerless tree leak) — no new processes or threads are created, so
  nothing new can be orphaned. The one adjacency is teardown: a backed-off
  watcher must not linger past `close()`, which is why teardown both sets the
  stop event and raises `_WakeSignal` (§3.2). Reviewed explicitly in Phase 3.
- **#2702's own "amplifier" note** — terminals kept alive via
  `keep_alive_after_exit` inflate the watcher count. Unaddressed here (it is the
  #2421/#1349 teardown gap), but each such watcher now costs ~1 % of what it did.

## 6. Explicitly not covered

- Reaping or capping live terminals / sessions (#1349, #2421).
- The codex / cursor / other native forwarders. They have the same shape and
  the same fix would apply, but each has its own state files and cursor
  semantics; landing one at a time keeps the blast radius reviewable.
- The permission-prompt spinner case (§3.2), which keeps a watcher at full rate.
- `_idle_watch_loop` (asyncio) backoff — no production caller.
- Replacing polling with true eventing. Every eventing option costs a process or
  a thread per unit, which is the thing being removed.

## 7. What the adversarial review caught

Reviewed as a hostile reader after the code landed; four findings changed the
implementation.

1. **A failing endpoint would have disabled the gate entirely.** The first cut
   held the gate open whenever any retry was *scheduled* (`has_pending()`) or
   `cost_retry_not_before` was non-zero. Those are set on failure and cleared
   only on success, so a permanently rejecting cost or events endpoint would
   have pinned every session at full-rate polling — the exact regression the
   change exists to prevent, triggered by an outage. Now the check is
   *due*, not *scheduled*: `has_due_retry(now)` and
   `0 < cost_retry_not_before <= now`.
2. **A client-interaction wake would not have reset the backoff.** Pane changes
   inside the client-interaction window are deliberately suppressed
   (`suppress_activity`), so the change path could not reset the interval for
   exactly the case the wake exists to serve. `_wait_next_idle_tick` now
   reports whether it was woken and the loop resets the interval on that.
3. **A wake storm could have become a fork storm.** `note_client_interaction`
   fires on every keystroke and mouse event. A single interruptible sleep would
   have let a user dragging the mouse drive tmux at input frequency. The sleep
   is now split: an un-interruptible `base_interval` floor, then the
   skippable backoff remainder. Poll rate is capped at the base rate by
   construction.
4. **A backed-off watcher could have lingered past teardown** — the
   ownerless-thread shape #2421 describes. `_stop_idle_watcher_thread` now sets
   the wake event alongside the stop event, and
   `test_threaded_idle_watcher_backoff_stops_promptly` asserts the thread is
   dead when the stop call returns.

Also fixed in review: `_tmux_executable` was cached for the process lifetime,
which would have returned a stale binary if `PATH` changed; it is now keyed on
`PATH`. `_capture_pane_for_idle_or_none` was renamed to
`_capture_pane_state_or_none` to match its new `(pane_dead, pane)` return, and a
docstring reference to the deleted `_pane_is_dead` was repointed.

Both test suites were mutation-checked: a fingerprint blinded to transcript
growth fails the two gate tests, and a backoff that grows without waiting for
post-idle quiescence fails
`test_threaded_idle_watcher_slow_output_never_reads_idle`.

### 7.1 Second round — cross-vendor review

1. **The turn-start wake never fired for native turns (blocking).** It was hung
   off `TerminalInstance.send`, but native harnesses inject through the bridge
   from the *harness process* (`claude_native_executor` →
   `inject_user_message`), which drives tmux over the socket and never touches
   the runner's instance. For the eight harnesses whose status edge is
   terminal-driven, an ordinary chat turn could start inside a backed-off
   sleep and the session would keep reporting `idle`. Moved to
   `_publish_turn_status(…, "running")`,
   which every dispatch path passes through — including the streaming branch
   that never reaches `_run_turn_bg`, an additional gap found while fixing
   this. Covered by an integration test through the real
   registry→watcher→publisher chain, plus an end-to-end test that POSTs a turn
   at the runner and asserts the wake reached the terminal.
2. **Backoff could delay the idle edge it was supposed to trail.** The loop
   timed quiescence from watcher start while the detector timed it from its
   first snapshot, so growth could fire one tick early. Growth now keys off
   `_IdleDetector.idle_notified`. The mutation test reproduces the original
   defect exactly: the edge lands at 1.05 s instead of 0.55 s.
3. **Fingerprint cost proportional to accumulated fan-out.** Halved by not
   stat'ing the write-once `agent-*.meta.json` files while still recording
   their names, and the residual is now measured and published as a budget
   (§4) rather than left implicit.
4. **Design promised transition logging that did not exist.** Implemented in
   both loops (§3.4).

Also added in this round: a wake grace window, without which the wake fixed in
(1) would have been undone by the watcher re-ramping during turn setup — the
wake marks output as *expected*, and setup can outlast the idle threshold.

### 7.2 Third round — cross-vendor review

No blocking findings. Three scoping/correctness fixes:

1. **The wake was too broad.** It woke *every* terminal of *every* harness, so
   a generic or auxiliary pane went to high-rate polling for a turn that had
   nothing to do with it. Now filtered to
   `PTY_STATUS_OWNING_TERMINAL_ROLES`, hoisted out of the watcher's inline set
   so the wake and the status gate read one definition. The route test had
   reinforced the wrong scope by asserting on a role-less pane; it now
   registers the claude-native role, and a negative test covers a generic pane
   and a codex-native one (whose forwarder, not its watcher, owns status).
2. **Every wake armed the full 15 s grace, and nothing released it.** Client
   interactions arrive per keystroke, so at the 0.2 s native base that was
   ~75 captures per interaction. The grace is now opt-in via
   `expect_output=True` (turn start only) and is released as soon as the pane
   changes — a normal turn pays a few captures, the full window only when the
   expected output never arrives.
3. **A wake landing between a timed-out `wait` and the `clear` was destroyed.**
   The caller was told it was not woken *and* the signal was gone, losing both
   the interval reset and the grace, which reopens the late-`running` race the
   wake exists to close. Only an observed wake is cleared now; an unobserved
   one stays set and costs at most one base interval. Covered by a boundary
   test using an event that fires the instant its wait times out.

While fixing (1) a block replacement over-captured and deleted three live
lines from `_start_terminal_activity_watcher` (the early return, `resource_id`,
and `loop`). Caught by the registry suite (`NameError: name 'loop' is not
defined`) and restored; the diff was then audited line by line to confirm
nothing else was lost.

### 7.3 Fourth round — cross-vendor review

No blocking findings. Two fixes:

1. **The wake and its reason were separate cross-thread state.** A
   `threading.Event` plus a `bool`, set and cleared independently: a turn wake
   landing between another wake's `clear` and the reason read had its halves
   consumed by different ticks. Walking the interleavings showed the grace
   still got armed in each one — but needing that walk at all was the defect.
   Both now live in `_WakeSignal` and are taken together by `consume()` under
   one lock, which also subsumes the round-3 "don't clear an unobserved wake"
   patch: there is no longer a window in which a wake can be reported absent
   *and* discarded. `expect_output` latches while a wake is pending, so a
   client interaction racing a turn cannot downgrade it. Covered by
   deterministic interleaving tests in both orders, plus retention and blocking
   cases; two mutations (unlatched reason, non-clearing consume) both fail
   them.
2. **The watcher docstring still described Claude/Pi only** while eight roles
   were supported. It now points at `PTY_STATUS_OWNING_TERMINAL_ROLES`.

### 7.3.1 Post-deploy: the detector became the cost

Field profile of the then-deployed fix on an idle runner reported the change
*detector* as the top leaf — the deployed build's
`_scan_into_fingerprint` and `_bridge_input_fingerprint` together ~41 % of
forwarder on-CPU time — with the caveat that the sample was small and the
ratios directional only. Those historical names are not present in the
current tree. Re-profiled
here over a 60 s steady-state window: the fingerprint was **61 %** of forwarder
CPU, so the report was right and if anything understated.

Two things came out of measuring rather than assuming:

- The suspicion that the failing `scandir` on a missing `subagents/` directory
  was expensive was **wrong** — 1.17 µs, noise.
- Making the benchmark's bridge directory realistic (it had 4 files where
  production has ~9-13, and no `message_deltas.jsonl`) exposed the
  self-triggering gate above. The forwarder rows in §4 Set 2 were measured
  against that sparse fixture, on both columns, and so were optimistic; §4
  Set 1 supersedes them on the realistic fixture.

The suggested fix of backing the poll interval off while idle was **declined**.
It trades streaming latency, and unlike the terminal watcher there is no wake
to recover it: `claude_native.py` runs `supervise_forwarder` in the native CLI
wrapper process, so the runner-side turn-start wake cannot reach it. Cheapening
the detector gets a larger win at zero latency cost.

### 7.3.2 Integration build: the mtime granularity assumption

The sub-agent membership cache keyed re-listing on the directory mtime
changing. That assumption held on the development box and failed on an
integration build, where
`test_bridge_input_fingerprint_picks_up_a_subagent_added_after_priming` failed
~90 % of runs: `st_mtime_ns` carries nanoseconds but Linux stamps directory
mtimes from the jiffy clock, so an entry created inside the recorded tick left
the mtime unchanged 194/200 times — and nothing moved it afterwards, so the
sub-agent stayed unwatched until the resync.

This was dismissed during design as "extremely unlikely (ns resolution)". The
error was reading `st_mtime_ns`'s *units* as its *resolution*. APFS misses
0/200, which is why local runs and every review round passed.

Fixed with a racy window (§3.3.1). The regression test pins the directory mtime
back with `os.utime` rather than racing for the collision, so it reproduces on
any filesystem instead of only on a coarse one — the property the original test
lacked.

The first attempt at that window was itself incomplete, in two ways review
caught by reproducing rather than reasoning: it did not carry the uncertainty
forward, so a change landing late in a coarse bucket could age out of the
window before the next tick and never be re-listed; and it used `abs()`, which
called a future-dated stamp "settled" once it was far enough ahead. Both now
have tests that fail against the versions that shipped past two review rounds.

Those tests drive `_fingerprint_now_ns` rather than sleeping past a real
window. The sleep-based first version had a failure mode of its own: a
scheduler pause during setup ages the fixture out of the window before the
first sample, so the test fails having never exercised the latch — noise
indistinguishable from the bug. The indirection follows the same reasoning as
`_supervisor_monotonic`, which exists so tests need not monkeypatch a module
singleton.

### 7.4 Fifth round — cross-vendor review

No blocking findings. Two fixes:

1. **`Condition.wait` was not predicate-looped.** `consume`'s timeout *is* the
   watcher's backed-off poll interval, so a `wait` returning before its
   timeout with nothing pending reads as "the interval elapsed" and forks tmux
   early — precisely the cost the backoff removes. Now
   `wait_for(lambda: self._pending, timeout)`, which loops against a monotonic
   deadline. Two tests drive a `Condition` subclass whose `wait` always
   returns early: one asserts the sleep is still served in full and the loop
   re-waited, the other that a real wake landing mid-loop is still taken
   promptly. Reverting to the bare `wait` fails both.
2. **`wake_idle_watcher`'s thread-safety note still described a
   `threading.Event`.** Updated to `_WakeSignal`'s lock and its retention
   guarantee.

The first spurious-wake stub held the condition's lock across its early
return, which deadlocked the thread posting the wake — the opposite of the
situation under test. It now releases and re-acquires around the return, as
the real `wait` does.

### 7.5 Rebase review — status-file ownership

Upstream added `SessionStatusPoller` after this design began. Once active for
claude-native, it is the sole source of running/idle status and is driven by
the watcher's `on_tick`. Applying the original backoff unchanged would have
stretched that authoritative poll from 0.2 s to as much as 2 s, delaying both
permission-dialog `waiting` changes and reconnect resync publication.

The interval policy is therefore caller-controlled: resource-registry passes
a predicate that forbids backoff while the file poller is active. The terminal
module only knows a generic dynamic policy, not session-status concepts. Tests
cover both sides: active file ownership stays at base cadence, while a watcher
without that ownership still backs off. The turn-start wake and grace remain
for PTY-only roles and for claude-native's fallback periods.

## 8. Residual risks

1. **Late `running` edge on autonomous pane output.** A backoff-eligible
   terminal that starts producing output with no preceding `send()` or client
   interaction is noticed up to `max_interval` late (up to 2 s at a 0.2 s
   base, 5 s at a 1 s base). Active claude-native status-file polling is not
   eligible. Turn start, typing, and attach are wake-triggered, so this only
   affects output originating entirely inside the pane after an idle period.
2. **A lagging filesystem clock defeats the racy window.** `_mtime_is_racy`
    measures a timestamp's age against the local clock, so a networked
    filesystem whose server clock *trails* ours by more than the window makes a
    fresh change look already settled, and membership falls back to mtime
    equality. Bounded by the resync. A *leading* clock is safe — the check is
    one-sided, so any future-dated stamp reads as untrustworthy at any
    distance.
3. **Fingerprint blind spot.** A same-inode, same-size, same-`mtime_ns` rewrite
   would be missed for up to `_IDLE_RESYNC_SECONDS`. No current writer can
   produce one; the resync bounds it regardless.
4. **Settle window is a constant, not a derivation.** If a future time-based
   transition longer than 8 s is added to the forwarder body without a matching
   deadline check, it would be delayed to the next resync. Mitigated by the
   constant carrying a comment naming what it must cover.
5. **Late `running` under slow sustained output.** A backoff-eligible pane that changes less
   often than its idle threshold already reads idle between changes (existing
   detector semantics, unchanged). Backoff additionally delays the following
   `running` edge by up to the ceiling. An active claude-native status poller
   remains at base cadence.
6. **A backed-off watcher reports a pane exit up to one interval late.** Only
   reachable for a pane that dies after ≥1 idle threshold of silence — i.e. a
   kill or crash, not a clean end-of-turn exit, which repaints first and so is
   seen at base rate.
7. **Merged tmux probe changes one error path.** Previously a failing
   `list-panes` was treated as "pane alive"; now a failing sequence is treated
   as "server gone" and fires `on_exit`. Both commands target the same session,
   so they succeed and fail together in practice, but a tmux that could fail
   `list-panes` while serving `capture-pane` would report an exit one interval
   early.
8. **Idle CPU still scales with accumulated fan-out** — ~0.65 % of a core per
   idle session at 100 sub-agents on the Set 2 macOS host, ~2.4 % on the Set 1
   Linux host (§4). Down ~11x, but not flat.
9. **The turn-start wake walks the session's terminal list on every turn.** It
   wakes nothing outside `PTY_STATUS_OWNING_TERMINAL_ROLES`, but the walk plus
   a role lookup per terminal happens for every session, native or not. At the
   sizes involved (a handful of terminals per session) that is a dict lookup
   and a short loop, once per turn.
10. **An unobserved wake costs one base interval.** A wake raised while the
   tick body is running stays pending rather than being destroyed, but it is
   serviced on the next sleep rather than immediately — up to 0.2 s late for
   the native watcher.
11. **The bridge-file classification contract is not fail-closed.** The
    by-name sweep misses a producer nobody listed; the live-session test
    misses a producer or branch its scenario does not reach; the disposition
    table is manually maintained; and the real-loop audit covers `Path.open`
    reads only on the exercised path (§3.3.1). An
    unwatched *input* would have its changes surface on the 10 s resync rather
    than the next tick: bounded staleness, not permanent loss. Whether that
    staleness is merely late or actually wrong depends on what the input means
    — the resync guarantees eventual observation, not correctness during the
    gap, so a future input carrying something time-critical would need
    watching, not just resyncing. An unwatched *output* is harmless, which is
    what every miss so far has been.
12. **The grace is released by any agent-attributed pane change, not only the
    awaited turn's output.** A status-bar repaint during turn setup ends the
    grace early, after which the ordinary idle threshold and backoff ramp
    apply — so detection falls back to the ceiling (≤2 s native) rather than
    staying pinned at base. Deliberate: `changed_this_tick` is the same signal
    that drives `on_activity`/`running`, and giving the grace a private,
    stricter notion of "real output" would put two definitions of activity in
    one loop. Client-driven repaints are already excluded by
    `suppress_activity`.
13. **Fingerprint I/O is outside the iteration stall deadline.** The gate must
    run before `asyncio.timeout(_FORWARD_LOOP_STALL_DEADLINE_S)` so an unchanged
    tick never enters that 300 s scope. Its synchronous `stat` calls and
    sub-agent `scandir` therefore have no deadline armed. A bridge directory on
    a hung filesystem can stall the forwarder silently and, because synchronous
    filesystem syscalls do not yield, can stall the event-loop thread; merely
    moving them inside `asyncio.timeout` would not preempt the syscall. Accepted
    because bridge directories are runner-local. Supporting remote or unreliable
    filesystems would require moving the fingerprint I/O to an interruptible
    worker and belongs in a separate architectural change.
