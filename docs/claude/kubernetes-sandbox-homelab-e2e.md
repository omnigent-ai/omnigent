# Kubernetes sandbox provider — homelab end-to-end validation & findings

Live validation of the `kubernetes` sandbox provider on the joyful.house k3s
cluster (issue #39), the agent-reply root-cause investigation, and the resulting
shipping decision.

## What was validated (full provider lifecycle)

A throwaway namespace `omnigent-k8s-test` ran the server from a branch image
built with the `kubernetes` extra (`OMNIGENT_EXTRAS=kubernetes`), sqlite,
auth off, `sandbox.provider=kubernetes`. Driving the real HTTP API
(`POST /v1/sessions` with `host_type=managed` → `POST /v1/sessions/{id}/events`
→ poll `/items`):

- **provision** — runner Pod created (`sleep ∞` under a PID-1 reaper,
  `runAsUser/Group/fsGroup 1000` + `OnRootMismatch`, writable `HOME` emptyDir,
  `automountServiceAccountToken: false`, creds via `envFrom`,
  `nodeSelector kubernetes.io/arch: amd64`), readiness awaited.
- **host registration** — server execs `omnigent host` into the Pod
  (`pods/exec`, `container=host`); host dials back over the launch-token tunnel.
- **turn dispatch** — user message dispatched to the runner; assistant replied.
- **terminate** — `DELETE /v1/sessions/{id}` deletes the runner Pod promptly.
- **RBAC** — namespaced Role (`pods` create/get/delete, `pods/exec` get+create,
  `events` list) is sufficient and least-privilege (verified with
  `kubectl auth can-i`).

**A real agent reply was captured** (`claude-sdk` agent → prompt "Reply with
exactly: OK" → assistant "OK") through a provider-spawned runner Pod.

## The agent-reply failure and root cause

The agent turn initially failed on some sessions with a Bun crash:

```
[N] embedder failed to suspend thread 0x... for TLC 0x...
panic: Segmentation fault at address 0x0
oh no: Bun has crashed.
```

It reproduced only on **some** runner Pods. The discriminator was the **node**,
not the seccomp profile. Controlled 2×2 (identical image, same securityContext,
`claude -p` ×3 per cell; seccomp mode confirmed live via `/proc/self/status`):

| Node / kernel | seccomp `Unconfined` | seccomp `RuntimeDefault` |
|---|---|---|
| **server2** — Ubuntu 24.04 / **6.8.0** (i7-6700K) | OK ×3 | OK ×3 |
| **server1** — Ubuntu 26.04 / **7.0.0** (i3-12100T) | segfault ×3 | segfault ×3 |

**Root cause:** an upstream incompatibility between Bun 1.3.14's JSC garbage
collector (signal-based thread suspension) and **Linux kernel 7.0.0**
(Ubuntu 26.04). It is **independent of the seccomp profile** — `Unconfined` does
not help. The provider cannot patch Bun.

Cluster amd64 node inventory at test time: `server2` = kernel 6.8 (good);
`server0`/`server1`/`server3` = kernel 7.0.0 (bad); the arm64 RPi nodes cannot
run the amd64-only host image.

> Note: an earlier in-session conclusion that "`seccompProfile: Unconfined` fixes
> the crash" was a **scheduler artifact** — the diagnostic Pod happened to land on
> the one kernel-6.8 node. The 2×2 above refutes it.

## The fix: node selection (verified)

The provider already supports a configurable `sandbox.kubernetes.node_selector`
(merged with the always-present `kubernetes.io/arch: amd64`). Pinning runner Pods
to a known-good-kernel node is the fix — no new provider code required.

Verified end-to-end: with `node_selector: {kubernetes.io/hostname: server2}` and
**no** seccomp override, a fresh managed session's runner Pod was scheduled on
server2 and the agent replied "OK" deterministically.

Recommended operator pattern (documented in the overlay README +
`sandbox-config.yaml`): label known-good nodes
(`kubectl label node <node> omnigent.ai/runner-ready=true`) and set
`node_selector: {omnigent.ai/runner-ready: "true"}`.

## Shipping decision

The `seccomp_profile` provider option (built earlier on the mistaken diagnosis)
was **removed** — it provided no benefit in the only real environment and a
security-weakening knob marketed as a Bun fix would mislead. The PR ships the
provider + the node-selection fix as documentation (the `node_selector` seam
already existed). A genuinely seccomp-caused environment can reintroduce the knob
later with real evidence.

## Operational notes (homelab)

- **server2 is currently the only schedulable amd64 node on a good kernel**, so
  agent runners are effectively limited to it until: Bun ships a kernel-7.0.0
  fix and the host image is rebuilt on it, the 7.0.0 nodes are downgraded, or
  more 6.x amd64 nodes are added. Treat as a capacity/SPOF constraint.
- **File upstream:** Bun JSC GC `embedder failed to suspend thread` segfault on
  Linux 7.0.0 (Ubuntu 26.04).
- The provider correctly surfaces the crash: provisioning/readiness pass (the Pod
  is healthy), and the Bun error appears in the session's error item when the
  turn runs — there is no way to pre-detect a Bun crash at provision time.
