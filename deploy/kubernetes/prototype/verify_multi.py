"""Run one local Kubernetes rollout with work active on several external hosts.

The existing verify.py exercises each host. Barriers keep every host active
through the same rollout and prevent cleanup until all recovery checks finish.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
import traceback
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from verify import KEY_HEADER, command, verify


def ready(pod: dict) -> bool:
    return any(
        condition["type"] == "Ready" and condition["status"] == "True"
        for condition in pod["status"].get("conditions", [])
    )


async def run(args) -> None:
    if urlsplit(args.url).hostname != "localhost":
        raise ValueError("Only the local prototype at localhost is supported")
    if args.replicas < 1 or args.hosts < args.replicas or args.hosts % args.replicas:
        raise ValueError("Use a positive replica count and an equal number of hosts per replica")
    args.output.mkdir(parents=True, exist_ok=False)
    kube = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        "kind-omnigent-nginx-prototype",
        "-n",
        "omnigent-prototype",
    ]
    started = time.monotonic()
    summary = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "replicas": args.replicas,
        "hosts_requested": args.hosts,
        "passed": False,
        "rollouts_triggered": 0,
        "pod_samples": [],
    }
    (args.output / "source-revision.txt").write_text(await command("git", "rev-parse", "HEAD"))
    (args.output / "local-test-changes.patch").write_text(
        await command("git", "diff", "--", "deploy/kubernetes/prototype/verify.py")
    )
    (args.output / "verify_multi.py").write_text(Path(__file__).read_text())
    deployment = json.loads(await command(*kube, "get", "deployment/omnigent", "-o", "json"))
    (args.output / "deployment-before.json").write_text(json.dumps(deployment, indent=2) + "\n")
    if not (deployment["spec"]["replicas"] == args.replicas):
        raise RuntimeError(
            f"Scale the local Deployment to {args.replicas} replicas before running this check"
        )
    summary["rolling_update_strategy"] = deployment["spec"]["strategy"]
    await command(*kube, "rollout", "status", "deployment/omnigent", "--timeout=180s")
    initial = json.loads(await command(*kube, "get", "pods", "-l", "app=omnigent", "-o", "json"))
    if not (len(initial["items"]) == args.replicas and all(ready(p) for p in initial["items"])):
        raise RuntimeError("Expected every server pod to be ready before the rollout")
    summary["initial_pods"] = [pod["metadata"]["name"] for pod in initial["items"]]
    summary["initial_images"] = {
        pod["metadata"]["name"]: pod["status"]["containerStatuses"][0]["imageID"]
        for pod in initial["items"]
    }
    by_upstream = {
        f"{pod['status']['podIP']}:8000": pod["metadata"]["name"] for pod in initial["items"]
    }

    groups: dict[str, list[str]] = {upstream: [] for upstream in by_upstream}
    per_pod = args.hosts // args.replicas
    async with httpx.AsyncClient(base_url=args.url, timeout=5, trust_env=False) as probe:
        for _ in range(512):
            host_id = uuid.uuid4().hex
            response = await probe.get("/health", headers={KEY_HEADER: host_id})
            response.raise_for_status()
            upstream = response.headers["x-omnigent-upstream"]
            if upstream in groups and len(groups[upstream]) < per_pod:
                groups[upstream].append(host_id)
            if all(len(hosts) == per_pod for hosts in groups.values()):
                break
    if not (all(len(hosts) == per_pod for hosts in groups.values())):
        raise RuntimeError(groups)
    host_args = []
    for upstream, host_ids in sorted(groups.items()):
        for host_id in host_ids:
            index = len(host_args) + 1
            host_args.append(
                SimpleNamespace(
                    label=f"host-{index}",
                    host_id=host_id,
                    expected_upstream=upstream,
                    initial_pod=by_upstream[upstream],
                    replicas=args.replicas,
                    kubeconfig=args.kubeconfig,
                    url=args.url,
                    mock_port=0,
                    output=args.output / f"host-{index}",
                )
            )
    summary["initial_distribution"] = {
        by_upstream[upstream]: hosts for upstream, hosts in groups.items()
    }
    print(json.dumps({"initial_distribution": summary["initial_distribution"]}), flush=True)

    logs = {}
    monitor_errors = []

    async def monitor():
        while True:
            try:
                snapshot = json.loads(
                    await command(*kube, "get", "pods", "-l", "app=omnigent", "-o", "json")
                )
                pods = []
                for pod in snapshot["items"]:
                    name = pod["metadata"]["name"]
                    terminating = bool(pod["metadata"].get("deletionTimestamp"))
                    pods.append(
                        {
                            "name": name,
                            "ready": ready(pod),
                            "terminating": terminating,
                            "ip": pod["status"].get("podIP"),
                        }
                    )
                    if ready(pod) and name not in logs:
                        handle = (args.output / f"{name}.log").open("w")
                        process = await asyncio.create_subprocess_exec(
                            *kube,
                            "logs",
                            name,
                            "--timestamps",
                            "--follow",
                            "--since=10s",
                            stdout=handle,
                            stderr=asyncio.subprocess.STDOUT,
                        )
                        logs[name] = (process, handle)
                summary["pod_samples"].append(
                    {"at": round(time.monotonic() - started, 3), "pods": pods}
                )
            except (RuntimeError, OSError, json.JSONDecodeError) as exc:
                monitor_errors.append(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(0.5)

    prepared = asyncio.Barrier(args.hosts)
    checked = asyncio.Barrier(args.hosts)
    rolled_out = asyncio.Event()
    rollout_started = 0.0

    async def coordinated_rollout() -> float:
        nonlocal rollout_started
        leader = await prepared.wait()
        if leader == 0:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as llm:
                gates = await asyncio.gather(
                    *(
                        llm.get(f"http://127.0.0.1:{host.mock_port}/gate/pending")
                        for host in host_args
                    )
                )
                if not (all(gate.is_success and gate.json()["pending"] for gate in gates)):
                    raise RuntimeError("A model request stopped waiting before the rollout began")
            summary["active_turns_at_rollout"] = len(gates)
            summary["hosts_ready_at_rollout"] = args.hosts
            print(
                f"All {args.hosts} hosts have an active turn and shell. Starting one rollout.",
                flush=True,
            )
            rollout_started = time.monotonic()
            summary["rollout_started_at"] = round(rollout_started - started, 3)
            await command(*kube, "rollout", "restart", "deployment/omnigent")
            summary["rollouts_triggered"] += 1
            status = await command(
                *kube, "rollout", "status", "deployment/omnigent", "--timeout=180s"
            )
            (args.output / "rollout.log").write_text(status)
            print(status, flush=True)
            summary["kubernetes_rollout_complete_at"] = round(time.monotonic() - started, 3)
            rolled_out.set()
        else:
            await rolled_out.wait()
        return rollout_started

    async def all_hosts_checked():
        await checked.wait()

    async def one_host(host):
        report = await verify(
            host,
            coordinated_rollout=coordinated_rollout,
            all_hosts_checked=all_hosts_checked,
        )
        if not (report["initial_host_upstream"] == host.expected_upstream):
            raise RuntimeError("Host requests reached a different replica before the rollout")
        if not (report["initial_runner_upstream"] == host.expected_upstream):
            raise RuntimeError("Runner requests reached a different replica before the rollout")

    monitor_task = asyncio.create_task(monitor())
    try:
        async with asyncio.timeout(300), asyncio.TaskGroup() as group:
            for host in host_args:
                group.create_task(one_host(host), name=host.label)
        final = json.loads(await command(*kube, "get", "pods", "-l", "app=omnigent", "-o", "json"))
        summary["final_pods"] = [pod["metadata"]["name"] for pod in final["items"]]
        if not (len(final["items"]) == args.replicas and all(ready(p) for p in final["items"])):
            raise RuntimeError("Expected every replacement server pod to be ready")
        if not (set(summary["initial_pods"]).isdisjoint(summary["final_pods"])):
            raise RuntimeError("An original server pod remains after the rollout")
        summary["passed"] = True
    except Exception:
        summary["error"] = traceback.format_exc()
        raise
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        for process, handle in logs.values():
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            handle.close()
        with contextlib.suppress(RuntimeError, OSError):
            (args.output / "nginx.log").write_text(
                await command(*kube, "logs", "deployment/nginx", "--timestamps", "--since=10m")
            )
        hosts = []
        for host in host_args:
            report_path = host.output / "report.json"
            report = (
                json.loads(report_path.read_text()) if report_path.exists() else {"passed": False}
            )
            hosts.append(
                {
                    "label": host.label,
                    "initial_pod": host.initial_pod,
                    **{key: value for key, value in report.items() if key != "samples"},
                }
            )
        summary["hosts"] = hosts
        summary["hosts_passed"] = sum(host.get("passed", False) for host in hosts)
        summary["initial_host_counts"] = dict(Counter(host["initial_pod"] for host in hosts))
        summary["final_host_counts_by_upstream"] = dict(
            Counter(host.get("final_upstream", "unknown") for host in hosts)
        )
        summary["monitor_errors"] = monitor_errors
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        samples = summary["pod_samples"]
        if samples:
            summary["minimum_ready_nonterminating_pods"] = min(
                sum(pod["ready"] and not pod["terminating"] for pod in sample["pods"])
                for sample in samples
            )
            summary["maximum_nonterminating_pods"] = max(
                sum(not pod["terminating"] for pod in sample["pods"]) for sample in samples
            )
        summary["passed"] = bool(summary["passed"] and summary["hosts_passed"] == args.hosts)
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(
            json.dumps(
                {
                    key: value
                    for key, value in summary.items()
                    if key not in {"pod_samples", "hosts", "initial_images"}
                },
                indent=2,
            ),
            flush=True,
        )
        print(f"Evidence: {args.output / 'summary.json'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--url", default="http://localhost:18081")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replicas", type=int, default=3)
    parser.add_argument("--hosts", type=int, default=6)
    asyncio.run(run(parser.parse_args()))
