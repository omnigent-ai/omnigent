# Host drain (launch fence)

A drain stops **new runner launches** on one host. It does not stop the runners
already there: running, waiting, and background sessions keep running, and
pending launches already admitted are honoured. Its job is to answer one
question with certainty: *is it safe to restart this host without losing a
launch?*

## Why "stop the daemon when it has zero runners" is not enough

A `host.launch_runner` frame is handled on its own task. The handler queues on
the lifecycle lock, runs preflight checks, then spawns the runner inside a
cancellation-shielded thread. The runner is only registered in the host's
`_runners` map **after** that thread returns. The server-side launch future
lives only in that replica's memory. So a snapshot of "no live runners" can be
taken while a launch is queued or mid-spawn, and a restart then loses it.

Pausing scheduled tasks does not help either: "run now" fires a paused task.

## Design

Three layers, each conservative on its own:

1. **Durable admission guard (server).** The fence is a set of columns on the
   `hosts` row (`drain_generation`, `drain_started_at`, …), so it survives a
   server restart and is shared by every replica. `HostStore.admit_launch`
   checks it while holding the host row lock (`SELECT … FOR UPDATE`), and
   `start_drain` takes the same lock, so a launch is either admitted before the
   drain commits or refused. Every launch path calls `admit_launch` before it
   writes a binding or sends a frame. `resolve_host_launch` and the session-
   create, scheduled-fire and managed-wake entry points also check
   `host.draining` early, before side effects such as worktree creation or a
   new session row.
2. **Receipt-time fence (host).** The server sends `host.set_drain
   {generation, draining}` on the host tunnel. The receive loop applies it
   inline, in frame order. Each launch frame is counted when the loop reads
   it, before it is handed to a task, the lock, preflight, or the shielded
   spawn. While draining, the loop refuses it with `error_code=host_draining`.
   A launch counts as pending until its handler settles. A cancelled spawn
   counts until its teardown task finishes. Frames on one tunnel are ordered,
   so every launch the server sent before the fence is counted and every one
   after it is refused. Nothing is invisible.
3. **Generation-bound close-ack.** The host answers every `host.set_drain`,
   and sends again whenever its pending count changes, with
   `host.drain_status {generation, draining, pending_launches, running_runners,
   complete}`. `complete` means draining and zero pending. The server records
   the ack only when that generation is still the current, active drain and
   the host's connection supports drain. Every drain and undrain bumps the
   generation, so a late ack can never satisfy a later drain. Every host
   reconnect clears the ack and resends the fence, so each connection must
   earn its own ack.

`safe_to_restart` is `draining AND drain_supported AND acked_generation ==
drain_generation`. Anything unknown reads **not safe**: an old host build, a
host that has not reconnected since the upgrade, a pending ack, or a malformed
report.

### Rejected launches

A rejected launch fails with `ErrorCode.HOST_DRAINING` (HTTP 503) and is never
retried onto another host. The one exception is a scheduled task with no
pinned host: its resolver already picks "the owner's first online host", and it
now skips draining hosts as part of that existing placement rule. A rejected
session create leaves no row behind. A rejected message is not consumed, so the
client can retry after undrain. A launch that passed admission but reached the
host after the fence is refused there, and the server maps that refusal to the
same `HOST_DRAINING` error.

## Operator surface

| | API | CLI |
|---|---|---|
| Start (idempotent) | `POST /v1/hosts/{id}/drain` | `omnigent host drain [HOST_ID] [--wait SECONDS]` |
| Status | `GET /v1/hosts/{id}/drain` | `omnigent host drain-status [HOST_ID]` (exit 0 only when safe) |
| Undrain | `DELETE /v1/hosts/{id}/drain` | `omnigent host undrain [HOST_ID]` |

Only the host's owner can call these endpoints. The status response includes
`state` (`active` / `draining` / `drained` / `unsupported`), `safe_to_restart`,
`generation` / `acked_generation`, `drain_support`, the host's own latest report
(`pending_launches`, `running_runners`), and `server_pending_launches` (this
replica's in-flight launch round-trips). The live report is available only on
the replica that holds the host's tunnel. The ack is in the database, so
`safe_to_restart` is correct from any replica.

Runbook for a host restart:

```bash
omnigent host drain <HOST_ID> --wait 300   # exits 0 once safe to restart
# restart the host (running runners on it stop with it; their sessions persist)
omnigent host undrain <HOST_ID>            # after the host reconnects
```

## Limitations

- **Bootstrap.** Installing this cannot protect launches already in flight on
  a host running an older build. That host never advertises `launch_drain`,
  so it reads `unsupported` and is never reported safe. Upgrade and restart the
  host once, the old way, then use drain for every later restart. The new
  columns start at `drain_supported = NULL` ("unknown"), which is also never
  safe.
- **Cross-replica delivery.** A drain requested on a replica that does not
  hold the host's tunnel reaches the host on that tunnel's next keepalive tick
  (≤ 30 s). The admission guard is enforced on every replica immediately.
- **Managed sandboxes.** A sandbox wake or re-provision that started before the
  drain committed keeps provisioning. Its runner launch is still fenced at
  admission, so no runner starts. New wakes and re-provisions are refused at
  entry.
- **Drain ≠ quiesce.** `running_runners` is reported but does not block
  `safe_to_restart`. Restarting the host still stops its live runners. Their
  sessions are durable and relaunch on the next message after undrain.
