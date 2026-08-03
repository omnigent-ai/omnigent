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

## 1. Mechanisms as they exist today

### 1.1 Terminal idle watcher (#2702)

`TerminalInstance.start_idle_watcher_thread` spawns one daemon thread named
`terminal-idle-<name>-<key>`. Each tick (`omnigent/inner/terminal.py:1627`):

1. `stop_event.wait(interval)` — the poll sleep.
2. `_capture_pane_for_idle_or_none()` → `subprocess.run(tmux … capture-pane)`.
3. `_pane_is_dead()` → `subprocess.run(tmux … list-panes -F '#{pane_dead}')`.
4. `_IdleDetector.tick(snapshot)` — pure diff/marker state machine.
5. Fires `on_activity` / `on_idle` / `on_exit`.

Two `subprocess.run` calls per tick. `_tmux_base_cmd()` uses the bare string
`"tmux"`, so `execvp` walks `PATH` — the issue's `strace` shows **4 `execve`
per invocation, 3 of them `ENOENT`**.

Two watcher configurations exist (`omnigent/runner/resource_registry.py:1111`):

| watcher | interval | idle threshold | callbacks |
|---|---|---|---|
| generic terminal | 1.0 s | 10.0 s | `on_activity`, `on_exit` |
| claude-native agent terminal | 0.2 s | 1.0 s | `on_activity`, `on_idle`, `on_exit` |

The asyncio sibling `_idle_watch_loop` has **no production caller** (only
`close()` stops it, and tests drive it). It is left alone apart from inheriting
the resolved tmux path.

#### Latency consumers

| consumer | source | budget |
|---|---|---|
| `session.terminal.activity` pulse (web activity badge) | `_on_activity` → `activity_publisher` | web keeps the badge lit for 1.5 s (`ACTIVE_OUTPUT_WINDOW_MS`); already throttled to 1 emit/s by `_TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS` |
| session `running` status (Agents panel) | `_on_activity` → `status_publisher` | human-visible; sub-second desirable at turn start |
| session `idle` status | `_on_idle` → `status_publisher` | fires on the 1.0 s threshold; also memoised for exit classification (`_set_session_status_memo`) |
| terminal exit lifecycle | `_on_exit` → `_handle_terminal_exit` | seconds-scale is fine; it publishes a lifecycle event and cleans up |

The critical observation: **the `running` edge that users actually notice is
turn start, and turn start is initiated by the runner itself** (`send()` writes
to the pane). The runner therefore already knows when to expect output — it
does not have to discover it by polling.

### 1.2 Claude transcript forwarder (#3000)

`forward_claude_transcript_to_session` (`omnigent/claude_native_forwarder.py:794`)
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

Strategy D's three parts multiply: `8 execve/tick → 1 execve/tick`, then
`5 ticks/s → 0.5 ticks/s` for the claude-native watcher.

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

**(a) Resolve the tmux binary once.** Module-level `functools.cache` over
`shutil.which("tmux")`, falling back to the bare name so the failure message is
unchanged when tmux is absent. Cuts `execve` 4 → 1 per invocation. No behaviour
change.

**(b) Merge the pane-dead probe into the capture.** One tmux command sequence:

```
tmux -S <sock> -f /dev/null display-message -p -t main '#{pane_dead}' \; capture-pane -t main -p -e
```

Verified against tmux 3.6b: live pane prints `0\n<pane>`, dead pane (under
`remain-on-exit`) prints `1\n<pane>` and still exits 0, vanished server exits 1.
Halves fork+exec per tick, and removes a real race — today the pane can die
between the capture and the probe, so the snapshot and the liveness verdict come
from different instants.

**(c) Post-idle interval backoff.** Per tick:

```
changed        → interval = base                     (activity: full rate)
quiescent ≥ idle_threshold → interval = min(interval × 2, max_interval)
otherwise      → interval unchanged
```

`max_interval = min(base × 10, 5.0)` → 2.0 s for the claude-native watcher
(base 0.2), 5.0 s for generic terminals (base 1.0).

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
A new per-watcher `threading.Event` is set by:

- `_publish_turn_status(conv, "running")` in the runner, via
  `SessionResourceRegistry.wake_session_terminal_watchers` — **the turn-start
  hook that matters.** Native harnesses do *not* reach the pane through
  `TerminalInstance.send`: the executor calls `inject_user_message` from the
  harness process, which drives tmux over the socket directly, so the runner's
  watcher sees nothing in-process. `_publish_turn_status` is the one point
  every dispatch path passes through — background turns, continuation turns,
  the recovery path, and the streaming branch that never reaches
  `_run_turn_bg`. The wake runs *before* that function's native-harness
  suppression, because the harnesses whose status edge is terminal-owned are
  exactly the ones that need it.
- `TerminalInstance.send()` — the runner typing into a pane directly (tool-
  driven terminals).
- `TerminalInstance.note_client_interaction()` — attach/detach, focus, mouse,
  keystroke, resize from the web terminal.
- `_stop_idle_watcher_thread()` — so teardown does not wait out a backed-off
  sleep.

**Scope.** The turn-start wake fires only for
`PTY_STATUS_OWNING_TERMINAL_ROLES` — the eight harnesses whose pane watcher
*is* the session's status. A generic shell or auxiliary pane drives only the
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

The sleep is split so a wake storm cannot become a fork storm:

```
phase 1: stop_event.wait(base_interval)          # mandatory, un-wakeable floor
phase 2: wake_event.wait(interval - base)        # only the extra backoff is skippable
```

Poll rate is therefore capped at `1/base` no matter how many wakes arrive — a
user dragging the mouse over an attached terminal cannot drive tmux faster than
today's rate. Phase 1 keeps the `close()` join window bounded by `base` exactly
as it is today; phase 2 is interrupted by the same `wake_event` that stop sets.

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
| assistant item held for deltas | 2.0 s | settle window |
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

So the inputs are now stat'ed by name, from `_WATCHED_BRIDGE_FILES`, with
`_FORWARDER_OWNED_BRIDGE_FILES` — the loop's own cursors plus the shared
dead-letter sink — deliberately excluded.

That trades the scan's self-maintaining property for an explicit contract,
guarded by two tests that fail for different mistakes:

- `test_fingerprint_classifies_every_declared_bridge_file` sweeps the `*_FILE`
  constants of the modules that write into a bridge dir — the bridge, the
  forwarder, the statusLine wrapper, the message-display hook, and the shared
  post-delivery module. Blind to a producer nobody listed.
- `test_fingerprint_classifies_every_file_a_live_session_writes` drives those
  producers and requires every file left in the directory to be classified.
  Blind to a producer or branch the scenario does not reach.

**Neither is fail-closed, and together they are not either** — they are two
partial nets over the same contract, and the residual is stated in §8. A
genuinely closed version would need every producer to take its filenames from
one registry, but nothing forces a producer to use a registry rather than a
literal, so that buys discoverability rather than a guarantee — at the cost of
threading a new dependency through five modules. Worth revisiting if the
producer set grows; not worth it for the current five.

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

Everything used is portable: `shutil.which`, `subprocess.run`, `threading.Event`,
`os.scandir`, `os.stat`. No `inotify`, no `kqueue`, no `/proc`. There is no
degraded path to fall back to because there is no platform-specific path.

`st_ino` and `st_mtime_ns` are meaningful on both APFS and ext4, but their
*resolution* is not portable and must not be assumed — see the granularity
discussion in §3.3.1. Timestamp-derived logic is written against a window, not
against equality, precisely because a nanosecond field can carry a 4 ms (or
1 s) value.

## 4. Measurement

`dev/benchmarks/native_poll_cpu.py`, two scenarios, both holding everything
genuinely idle (no turns, no pane output, no transcript growth):

```bash
uv run python -m dev.benchmarks.native_poll_cpu terminal --terminals 8 --seconds 30
uv run python -m dev.benchmarks.native_poll_cpu forwarder --sessions 8 --seconds 30 --fanout 6
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

Pre-change source vs this branch, macOS 27 / M-series, Python 3.12.13, 30 s
window, everything idle:

| scenario | before | after | factor |
|---|---|---|---|
| terminal, poll 0.2 s (8 terminals) | **6.17 %** of a core / terminal | **0.42 %** | **15x** |
| terminal, tmux invocations | 8.57 /s / terminal | 0.50 /s | **17x** |
| terminal, `execve` (4 per invocation → 1) | ~34 /s / terminal | 0.50 /s | **69x** |
| forwarder, no fan-out (8 sessions) | **0.71 %** of a core / session | **0.12 %** | **6.1x** |
| forwarder, fan-out 6 (8 sessions) | **1.47 %** of a core / session | **0.15 %** | **9.9x** |
| forwarder, fan-out 100 (4 sessions) | **7.05 %** of a core / session | **0.65 %** | **10.9x** |

Extrapolating to the reported host (~20 sessions with fan-out): the forwarder
alone goes from ~29 % of a core to ~3 %, and 16 live terminals from ~99 % to
~6.7 %. That is consistent with the 20-25 %/runner attributed to each loop in
the issues.

### Detector fix (§3.3.1), re-measured on the realistic fixture

Deployed code vs this branch, 60 s window, 8 sessions (4 at fan-out 100),
everything idle:

| scenario | deployed | after | factor |
|---|---|---|---|
| forwarder, no fan-out | **0.47 %** of a core / session | **0.13 %** | **3.7x** |
| forwarder, fan-out 6 | **0.79 %** of a core / session | **0.17 %** | **4.6x** |
| forwarder, fan-out 100 | **2.85 %** of a core / session | **0.65 %** | **4.4x** |
| gate engagement, no fan-out | 54 % of ticks | **97.5 %** | — |

Most of that is the gate finally settling rather than the detector being
cheaper; the fingerprint itself is ~2x cheaper (28.7 → 14.1 µs at fan-out 0,
252 → 149 µs at fan-out 100). The remaining 2.5 % of ungated ticks is the
10 s resync, as designed.

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
idle session at fan-out 100**, i.e. a session with 100 accumulated sub-agents
costs about as much as four fan-out-6 sessions. Removing that would require
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
  watcher must not linger past `close()`, which is why stop sets the wake event
  (§3.2). Reviewed explicitly in Phase 3.
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
   terminal-owned, that watcher is the *only* running/idle source, so an
   ordinary chat turn could start inside a backed-off sleep and the session
   would keep reporting `idle`. Moved to `_publish_turn_status(…, "running")`,
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

Field profile of the shipped fix on an idle runner reported the change
*detector* as the top leaf — `_scan_into_fingerprint` and
`_bridge_input_fingerprint` together ~41 % of forwarder on-CPU time — with the
caveat that the sample was small and the ratios directional only. Re-profiled
here over a 60 s steady-state window: the fingerprint was **61 %** of forwarder
CPU, so the report was right and if anything understated.

Two things came out of measuring rather than assuming:

- The suspicion that the failing `scandir` on a missing `subagents/` directory
  was expensive was **wrong** — 1.17 µs, noise.
- Making the benchmark's bridge directory realistic (it had 4 files where
  production has ~9-13, and no `message_deltas.jsonl`) exposed the
  self-triggering gate above. The original "after" numbers in §4 were
  measured against that sparse fixture and so were optimistic.

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
`_supervisor_monotonic` and `_hold_monotonic`, which exist so tests need not
monkeypatch a module singleton.

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

## 8. Residual risks

1. **Late `running` edge on autonomous pane output.** A terminal that starts
   producing output with no preceding `send()` or client interaction is noticed
   up to `max_interval` late (2 s claude-native, 5 s generic). Turn start,
   typing, and attach are all wake-triggered, so this only affects output that
   originates entirely inside the pane after ≥1 idle threshold of silence.
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
5. **Late `running` under slow sustained output.** A pane that changes less
   often than its idle threshold already reads idle between changes (existing
   detector semantics, unchanged). Backoff additionally delays the following
   `running` edge by up to the ceiling. Bounded at 2 s claude-native / 5 s
   generic.
6. **A backed-off watcher reports a pane exit up to one interval late.** Only
   reachable for a pane that dies after ≥1 idle threshold of silence — i.e. a
   kill or crash, not a clean end-of-turn exit, which repaints first and so is
   seen at base rate.
7. **Merged tmux probe changes one error path.** Previously a failing
   `list-panes` was treated as "pane alive"; now a failing sequence is treated
   as "server gone" and fires `on_exit`. Both commands target the same session,
   so they succeed and fail together in practice, but a tmux that could fail
   `display-message` while serving `capture-pane` would report an exit one
   interval early.
8. **Idle CPU still scales with accumulated fan-out** — ~0.65 % of a core per
   idle session at 100 sub-agents (§4). Down ~11x, but not flat.
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
    misses a producer or branch its scenario does not reach (§3.3.1). An
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
