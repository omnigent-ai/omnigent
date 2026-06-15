# Implementation spec: `kubernetes` sandbox provider (Option A, codex-hardened)

Source of truth for the subagent-driven implementation. Reference impls to mirror:
`omnigent/onboarding/sandboxes/daytona.py`, `islo.py`, `modal.py`.

> **As-built note.** This is the original plan; a few details were refined during
> implementation and review. The authoritative as-built artifacts are the code,
> the overlay under `deploy/kubernetes/overlays/sandbox-runners/` (RBAC is split
> one-resource-per-file — `serviceaccount-*.yaml`, `role.yaml`, `rolebinding.yaml`
> — and the Role was narrowed to `pods` create/get/delete, `pods/exec` get+create,
> `events` list), and the live findings in
> [`kubernetes-sandbox-homelab-e2e.md`](kubernetes-sandbox-homelab-e2e.md)
> (notably: the agent-harness Bun crash is a kernel-7.0.0 issue fixed by
> `node_selector`, not seccomp).

## What we're building
A server-managed `sandbox.provider: kubernetes` that spawns an **agent runner Pod on demand**,
plugging into the EXISTING managed-host launch-token seam (Modal/Daytona/islo/cwsandbox). The Pod
boots `sleep infinity` (under a tiny PID-1 reaper); the server execs into it (`pods/exec`) to start
`omnigent host`, which dials back over the launch-token tunnel. Replaces the on-cluster `host/`
Deployment stopgap (#135).

## Decisions locked
- **Option A** (sleep∞ + pod-exec). Reuse the existing `_start_host_in_sandbox` orchestration with
  ZERO changes to it (lowest regression risk to other providers — the reason A was chosen).
- Reuse `DEFAULT_HOST_IMAGE = ghcr.io/omnigent-ai/omnigent-host:latest` (overridable). amd64-only.
- Agent classes: `claude-sdk` + `codex` parity (no native-ui). Creds via K8s Secret `envFrom`.
- In-cluster ServiceAccount config primary; kubeconfig fallback.
- `kubernetes` python client is an OPTIONAL dep (lazy import), pinned `>=36,<37` (latest 36.0.2).
- Token TTL: 7 days (`KUBERNETES_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600`) — pods have no platform cap;
  mirror Daytona/Islo policy.

## Codex review corrections to honor (folded into this spec)
- **M1**: Do NOT trust `WSClient.returncode`. Parse the exec **STATUS frame** (ERROR_CHANNEL=3)
  to get the real exit code. See `_parse_exec_status` below.
- **M2**: The image WORKDIR is `/root` (root-owned, no `/home/user`); `_start_host_in_sandbox`
  runs `printf %s "$HOME"` then `mkdir -p $HOME/workspace`. So the Pod MUST expose a **writable
  HOME**: set `HOME=/home/omnigent` env + mount an `emptyDir` at `/home/omnigent` + `fsGroup: 1000`
  + container `workingDir: /home/omnigent`.
- **M3**: Plain `sleep infinity` as PID 1 has no zombie reaper (the host re-parents orphaned runner
  procs to PID 1). Pod `command` must be a tiny **PID-1 reaper** that supervises `sleep infinity`
  and reaps children + forwards SIGTERM. Run it via `bash -lc "exec python3 -c '<reaper>'"`.
- **M4**: `automountServiceAccountToken: false` on the sandbox Pod (so a compromised agent can't use
  the server SA's `pods/exec` rights).
- **S2**: Wait for container **readiness** (`status.containerStatuses[*].ready`/`state.running`),
  not just `phase == Running`, before the first exec; retry transient "container not found" briefly.
- **S3**: Do NOT mutate global kube config. Build an isolated `client.Configuration()`, load
  in-cluster (or kubeconfig) INTO it, and construct `ApiClient(configuration)` + `CoreV1Api`.
- **S5**: All execs use `["bash", "-lc", command]` (the venv activates via `/etc/profile.d` only on
  login shells; `omnigent` must be on PATH).
- **Fast-fail** in the pod-ready wait on terminal/stuck states: `Failed`/`Succeeded` phase;
  container `waiting.reason` in {`ErrImagePull`,`ImagePullBackOff`,`InvalidImageName`,
  `CreateContainerConfigError`}; `PodScheduled=False` with `reason=Unschedulable`. Surface recent
  Pod events in the error. The pod-ready budget (~90s) is consumed inside `provision()` BEFORE the
  shared `_wait_for_host_online` 120s poll.
- **N1**: DNS-label-safe Pod names (mirror islo `_new_sandbox_name`: `omnigent-{slug[:40]}-{uuid6}`).
- Idempotent `terminate` (404 == success), `grace_period_seconds=0`.

## DELIBERATE DIVERGENCE from codex (do NOT implement)
- **M5** (launch-liveness check) would require editing the SHARED `_start_host_in_sandbox` launch
  command, which would change behavior/latency for Modal/Daytona/islo/cwsandbox. We chose Option A
  precisely to avoid shared-seam changes, so **leave `_start_host_in_sandbox` untouched**. The
  common failure modes (unschedulable, bad image, crash-loop) are already caught fast by the
  pod-ready wait; the rare "host boots then immediately dies" case falls through to the 120s online
  timeout. Documented as a future cross-provider follow-up.

## The launcher does NOT set host env vars
`OMNIGENT_HOST_TOKEN/ID/NAME` and the tunnel/header contract are injected by the UNCHANGED
`_start_host_in_sandbox` (via the `env_prefix` it builds and passes to `launcher.run()`). The
launcher only implements `prepare`/`provision`/`run`/`terminate`. Do not reference host-identity
constants in the launcher.

---

## Task I1 — Core launcher + unit tests
**Create** `omnigent/onboarding/sandboxes/kubernetes.py` and
`tests/onboarding/sandboxes/test_kubernetes.py`.

### Module shape (mirror daytona.py/islo.py conventions: module docstring, constants, `_ensure_sdk`)
Constants (env overrides, like daytona/islo):
- `HOST_IMAGE_ENV_VAR = "OMNIGENT_KUBERNETES_HOST_IMAGE"`
- `NAMESPACE_ENV_VAR = "OMNIGENT_KUBERNETES_NAMESPACE"` (default `"omnigent-sandboxes"` — the
  dedicated runner namespace the deploy overlay grants the server SA rights in; creating Pods in the
  server namespace would 403 + defeat the blast-radius split)
- `SANDBOX_SECRET_ENV_VAR = "OMNIGENT_KUBERNETES_SECRET"` (K8s Secret name for harness creds)
- `SANDBOX_ENV_PASSTHROUGH_ENV_VAR = "OMNIGENT_KUBERNETES_SANDBOX_ENV"` (comma-sep server env NAMES)
- `SERVICE_ACCOUNT_ENV_VAR = "OMNIGENT_KUBERNETES_SERVICE_ACCOUNT"` (default `"omnigent-runner"`)
- `KUBECONFIG_ENV_VAR = "OMNIGENT_KUBERNETES_KUBECONFIG"` (optional kubeconfig path)
- sizing: `_SANDBOX_CPU_REQUEST="500m"`, `_SANDBOX_CPU_LIMIT="2"`, `_SANDBOX_MEMORY_REQUEST="1Gi"`,
  `_SANDBOX_MEMORY_LIMIT="4Gi"`, `_POD_READY_TIMEOUT_S=90`, `_POD_READY_POLL_S=2.0`, uid/gid `1000`,
  `_HOME_DIR="/home/omnigent"`.

### `_ensure_sdk()`
`try: import kubernetes` else `raise click.ClickException("The Kubernetes client is required for the
'kubernetes' sandbox provider. Install it with \`pip install 'omnigent[kubernetes]'\`.")`.

### PURE function `build_pod_manifest(...) -> dict[str, object]` (no SDK import, no I/O)
This is the primary unit-test surface. Params: `pod_name, namespace, image, service_account,
harness_secret: str | None, env_literals: dict[str,str], node_selector: dict[str,str] | None`.
Returns a plain dict Pod manifest:
- `metadata`: name, namespace, labels `{"app.kubernetes.io/managed-by":"omnigent",
  "omnigent.ai/role":"sandbox-host"}`.
- `spec.restartPolicy = "Never"`, `spec.automountServiceAccountToken = False`,
  `spec.serviceAccountName = service_account`,
  `spec.nodeSelector = {"kubernetes.io/arch":"amd64", **(node_selector or {})}`,
  `spec.securityContext = {runAsUser:1000, runAsGroup:1000, fsGroup:1000,
  fsGroupChangePolicy:"OnRootMismatch"}`,
  `spec.volumes = [{"name":"home","emptyDir":{}}]`.
- `spec.containers[0]`:
  - `name:"host"`, `image`, `workingDir:_HOME_DIR`,
  - `command`: PID-1 reaper, e.g.
    `["bash","-lc","exec python3 -c " + shlex.quote(_REAPER_SRC)]` where `_REAPER_SRC` spawns
    `subprocess.Popen(["sleep","infinity"])`, installs SIGTERM/SIGINT handlers that terminate the
    child, and loops `os.wait()` reaping all children until the child exits (codex M3 snippet).
  - `env`: `[{"name":"HOME","value":_HOME_DIR}, {"name":"IS_SANDBOX","value":"1"}]` + one entry per
    `env_literals` item (the resolved server-env passthrough).
  - `envFrom`: `[{"secretRef":{"name":harness_secret}}]` if `harness_secret` else `[]`.
  - `resources`: requests/limits from the sizing constants.
  - `securityContext`: `{allowPrivilegeEscalation:False}` (NOT readOnlyRootFilesystem — host writes
    /tmp + ~/.omnigent).
  - `volumeMounts`: `[{"name":"home","mountPath":_HOME_DIR}]`.

### `_new_pod_name(label) -> str`
Mirror islo `_new_sandbox_name`: lowercase, `[^a-z0-9-]→-`, collapse `-`, strip, `or "host"`,
return `f"omnigent-{base[:40]}-{uuid.uuid4().hex[:6]}"`.

### `_parse_exec_status(status_frames: list[str], pod: str) -> int`  (codex M1)
Join frames, `yaml.safe_load`; `{"status":"Success"}` → 0; else find
`details.causes[*].reason=="ExitCode"` → `int(message)`; else raise RuntimeError (no exit code).

### `KubernetesSandboxLauncher(SandboxLauncher)`
- ClassVars: `provider="kubernetes"`, `supports_cli_bootstrap=False`,
  `supports_local_port_forward=False`.
- `__init__(*, image=None, namespace=None, env=None, secret_name=None, node_selector=None,
  service_account=None, kubeconfig=None, in_cluster=None)`. Store; `self._core=None`,
  `self._api_client=None`.
- `_load_core() -> CoreV1Api` (S3): if cached return; build `cfg = client.Configuration()`; if
  `in_cluster is True` → `config.load_incluster_config(client_configuration=cfg)`; elif `False` →
  `config.load_kube_config(config_file=<kubeconfig or env or None>, client_configuration=cfg)`;
  else try in-cluster, `except config.ConfigException:` kubeconfig. Wrap failures in ClickException
  (mention in-cluster SA vs KUBECONFIG). `self._api_client = client.ApiClient(cfg)`;
  `self._core = client.CoreV1Api(self._api_client)`.
- `_resolve_image/_resolve_namespace/_resolve_secret/_resolve_service_account`: constructor value →
  env var → default (None for secret).
- `_resolve_sandbox_env() -> dict[str,str]`: COPY islo/daytona `_resolve_sandbox_env` (names from
  ctor or `SANDBOX_ENV_PASSTHROUGH_ENV_VAR`; values from server `os.environ`; fail loud on missing).
- `prepare()`: `_ensure_sdk()` then `_load_core()` (validates cluster reachable; raise ClickException
  with remediation).
- `provision(name) -> str`: `_ensure_sdk()`; build `env_literals=self._resolve_sandbox_env()`;
  `pod_name=_new_pod_name(name)`; `manifest=build_pod_manifest(...)`;
  `self._load_core().create_namespaced_pod(namespace, manifest)` (catch `ApiException` → friendly
  ClickException incl. `.reason`/`.body`; 403→RBAC hint, 409→regenerate name once); then
  `self._wait_for_pod_ready(pod_name)`; return `pod_name`.
- `_wait_for_pod_ready(pod_name)`: poll `read_namespaced_pod` every `_POD_READY_POLL_S` up to
  `_POD_READY_TIMEOUT_S`; READY when phase Running AND a container_status `.ready` True (S2);
  fast-fail per the rules above (terminal phase; imagepull/config waiting reasons; Unschedulable
  condition); on timeout include last `list_namespaced_event(field_selector=
  f"involvedObject.name={pod_name}")` reason/message. All errors → ClickException with
  `kubectl describe pod` hint.
- `run(sandbox_id, command, *, check=True) -> RemoteCommandResult` (M1, S5):
  `from kubernetes.stream import stream; from kubernetes.stream.ws_client import (ERROR_CHANNEL,
  STDOUT_CHANNEL, STDERR_CHANNEL)`. `ws=stream(core.connect_get_namespaced_pod_exec, sandbox_id,
  namespace, command=["bash","-lc",command], stderr=True,stdin=False,stdout=True,tty=False,
  _preload_content=False)`. Loop `while ws.is_open(): ws.update(timeout=1); read STDOUT/STDERR/ERROR
  channels via ws.read_channel(...)`; after close, drain remaining channels; `ws.close()`.
  `returncode=_parse_exec_status(error_frames, sandbox_id)`. Echo non-empty stdout/stderr lines
  (like daytona). If `check and returncode!=0` → ClickException(exit, command). Return
  `RemoteCommandResult(returncode, stdout, stderr)`. Wrap `ApiException` (e.g. pod deleted mid-run)
  → ClickException.
- `terminate(sandbox_id)`: `_ensure_sdk()`; `delete_namespaced_pod(sandbox_id, namespace,
  grace_period_seconds=0)`; `except ApiException` 404→return (idempotent), else ClickException.

### Tests `tests/onboarding/sandboxes/test_kubernetes.py`
- PURE: `build_pod_manifest` asserts — restartPolicy Never, automountServiceAccountToken False,
  nodeSelector contains arch amd64 (+ merged operator selector), securityContext uid/gid/fsGroup
  1000 + OnRootMismatch, HOME emptyDir volume + mount + HOME env + workingDir, envFrom secretRef when
  secret set (and `[]` when None), env_literals present, IS_SANDBOX=1, command is a reaper invoking
  sleep infinity, allowPrivilegeEscalation False.
- PURE: `_new_pod_name` DNS-safe (lowercases, strips bad chars, ≤63, unique suffix).
- PURE: `_parse_exec_status` → 0 on Success; correct code from ExitCode cause; raises when absent.
- Fake-SDK (inject a fake `kubernetes` module + submodules into `sys.modules`, mirroring
  test_modal.py's fake-injection style): `provision` creates a pod & returns name & waits ready;
  fast-fail on ImagePullBackOff / Unschedulable; `run` returns parsed returncode & raises on
  non-zero when check; `terminate` idempotent on 404; `prepare` in-cluster→kubeconfig fallback;
  config built into an isolated Configuration (no global mutation).
- `available_providers()` includes `"kubernetes"`.

---

## Task I2 — Wiring, config, dependency
**Modify** `omnigent/onboarding/sandboxes/__init__.py`: add to `_LAUNCHERS`:
`"kubernetes": "omnigent.onboarding.sandboxes.kubernetes:KubernetesSandboxLauncher"`.

**Modify** `omnigent/server/managed_hosts.py`:
- Add `"kubernetes"` to `SUPPORTED_SANDBOX_PROVIDERS` and `PROVIDERS_WITH_MANAGED_LAUNCH`.
- Add `KUBERNETES_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600` (comment: pods have no platform cap; policy
  bound mirrors Daytona/Islo).
- Add `_kubernetes_launcher_factory(image, env, namespace, secret_name, service_account,
  node_selector) -> Callable[[], SandboxLauncher]` (lazy import inside `_build`, mirror
  `_daytona_launcher_factory`).
- In `parse_sandbox_config`, add `elif provider == "kubernetes":` using the GENERIC provider
  helpers already in the file: `_parse_provider_image(raw,"kubernetes")`,
  `_parse_provider_env(raw,"kubernetes")`, `_parse_provider_string(raw,"kubernetes","namespace")`,
  `_parse_provider_string(raw,"kubernetes","secret_name")`,
  `_parse_provider_string(raw,"kubernetes","service_account")`, and a node_selector mapping. If no
  generic mapping parser exists, add `_parse_provider_str_mapping(raw, provider, key)` next to the
  others (validate dict[str,str], non-empty keys/values) — match the existing helpers' style/raises.
  Set `token_ttl_s = KUBERNETES_MANAGED_TOKEN_TTL_S`.
- Update the module-docstring YAML example block to add a `kubernetes:` section.

**Modify** `pyproject.toml`:
- Under `[project.optional-dependencies]` (after `daytona`/`cwsandbox`): `kubernetes =
  ["kubernetes>=36,<37"]` with a comment matching the modal/daytona extra style.
- Add mypy override: `[[tool.mypy.overrides]] module = "kubernetes.*" ignore_missing_imports = true`.

**Create** `tests/server/test_managed_hosts_kubernetes.py`: `parse_sandbox_config` for kubernetes —
minimal (`provider`+`server_url` only → defaults, `managed_launch_supported=True`,
`provider="kubernetes"`, `token_ttl_s==KUBERNETES_MANAGED_TOKEN_TTL_S`); full `kubernetes:` block
(image/namespace/secret_name/service_account/node_selector parsed); malformed values fail loud;
factory builds a `KubernetesSandboxLauncher`.

After editing pyproject: `OMNIGENT_SKIP_WEB_UI=true uv sync --extra all --extra dev --extra
kubernetes`. If `ap-web/package-lock.json` shows as modified, `git checkout --
ap-web/package-lock.json` (npm build side-effect, not part of this change).

---

## Task I3 — Deploy manifests + docs
**Create** `deploy/kubernetes/` (fold the server manifests from branch
`feat/kubernetes-deploy-rework` — read via `git show feat/kubernetes-deploy-rework:deploy/kubernetes/
server/<file>`; keep their hardening). Files:
- `namespace.yaml` (ns `omnigent`).
- `rbac.yaml`: ServiceAccount `omnigent-server` (the server runs as this) + ServiceAccount
  `omnigent-runner` (sandbox pods; needs no API access) + Role `omnigent-sandbox-manager`
  (`pods`: create/get/list/watch/delete; `pods/exec`: create; `pods/log`: get; `events`: list) +
  RoleBinding (server SA → Role). Namespaced (NOT ClusterRole). Comment the `pods/exec` rationale.
- `secret.example.yaml`: Opaque Secret `omnigent-creds` with `stringData` placeholders
  (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, OPENAI_API_KEY, CODEX_ACCESS_TOKEN, GEMINI_API_KEY,
  GIT_TOKEN, GIT_USERNAME) — annotated, referenced by `sandbox.kubernetes.secret_name`.
- Server manifests folded from #135: `server/deployment.yaml` (runs as `omnigent-server` SA),
  `server/service.yaml`, `server/pvc.yaml`, `server/configmap.yaml`, `server/secret.example.yaml`,
  `server/ingress.example.yaml`, `server/namespace.yaml` — keep their securityContext hardening &
  accounts-auth notes. (De-duplicate the namespace if both define it.)
- `config.yaml.example`: server config with the `sandbox: {provider: kubernetes, server_url: ...,
  kubernetes: {namespace, secret_name, service_account, node_selector}}` block.
- `README.md` (operator guide: apply order, secret creation, in-cluster SA vs kubeconfig, amd64-only,
  homelab K3s quickstart, troubleshooting: scheduling/imagepull/host-not-online, how to inspect
  `/tmp/omnigent-host.log` via `kubectl exec`), `SKILL.md` (match `deploy/docker/SKILL.md` format).
**Modify** `deploy/README.md`: add the `kubernetes/` row to the tree + provider table.
This task touches only `deploy/` — no Python, no tests beyond `kubectl apply --dry-run=client`
validation (run if kubectl available).

## Conventions (all tasks)
- `uv` only (never pip). `OMNIGENT_SKIP_WEB_UI=true` on syncs. Python 3.13 features OK (project is
  3.12+). NO lint-rule disables / `# type: ignore` / `# noqa` — fix root cause.
- Validate: `uv run ruff check --fix && uv run ruff format && uv run mypy --strict <files> &&
  uv run pytest <relevant tests>`.
- Match surrounding code's docstring density and style (these modules are heavily docstringed).
