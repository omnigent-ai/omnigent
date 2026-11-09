"""Validate the manual driver's lifecycle assertions without provisioning AWS."""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace

import boto3
import httpx
import pytest
from botocore.stub import Stubber

from tests.e2e.integrations.deploy.lambda_microvm import e2e_managed as driver


@pytest.mark.parametrize(
    "fault", [None, "not_suspended", "still_running", "no_new_reply", "replacement", "marker_lost"]
)
def test_driver_requires_same_vm_and_workspace(
    monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    client = boto3.client(
        "lambda-microvms",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    monkeypatch.setattr(driver, "_microvm_client", lambda: client)
    monkeypatch.setattr(
        sys, "argv", ["driver", "--server", "https://server.test", "--suspend", "--timeout", "5"]
    )
    clock = 0.0

    def advance(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    monkeypatch.setattr(driver, "time", SimpleNamespace(monotonic=lambda: clock, sleep=advance))
    followed_up = False
    deleted = False
    marker = ""

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal followed_up, deleted, marker
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(
                200, json={"managed_sandboxes_enabled": True, "sandbox_provider": "lambda_microvm"}
            )
        if path == "/v1/agents":
            return httpx.Response(200, json={"data": [{"id": "agent"}]})
        if path == "/v1/sessions" and request.method == "POST":
            return httpx.Response(200, json={"id": "session"})
        if path == "/v1/sessions/session/events":
            followed_up = True
            return httpx.Response(200, json={})
        if path == "/v1/sessions/session" and request.method == "DELETE":
            deleted = True
            return httpx.Response(200, json={})
        if path == "/v1/sessions/session":
            # An old reply containing 6 must not satisfy the follow-up deadline.
            replies = (
                ["4 and 6"] if fault == "no_new_reply" else (["4", "6"] if followed_up else ["4"])
            )
            return httpx.Response(
                200,
                json={
                    "host_id": "host",
                    "status": "idle",
                    "items": [
                        {
                            "type": "message",
                            "data": {
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": text}],
                            },
                        }
                        for text in replies
                    ],
                },
            )
        if path == "/v1/hosts/host/directories":
            import json

            marker = json.loads(request.content)["path"]
            return httpx.Response(200, json={"object": "directory", "path": marker})
        if path == f"/v1/hosts/host/filesystem/{marker}":
            if followed_up and fault == "marker_lost":
                return httpx.Response(404, json={"detail": "missing directory"})
            return httpx.Response(200, json={"object": "list", "data": []})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    image = {
        "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:test",
        "imageVersion": "1",
        "startedAt": datetime.datetime.now(datetime.UTC),
    }
    vm = {
        **image,
        "microvmId": "original",
        "endpoint": "https://vm.test",
        "maximumDurationInSeconds": 28800,
    }
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        monkeypatch.setattr(driver.httpx, "get", http.get)
        monkeypatch.setattr(driver.httpx, "post", http.post)
        monkeypatch.setattr(driver.httpx, "delete", http.delete)
        with Stubber(client) as stub:
            stub.add_response("list_microvms", {"items": []}, {})
            stub.add_response(
                "list_microvms",
                {"items": [{**image, "microvmId": "original", "state": "RUNNING"}]},
                {},
            )
            stub.add_response("suspend_microvm", {}, {"microvmIdentifier": "original"})
            state = {"not_suspended": "TERMINATED", "still_running": "RUNNING"}.get(
                fault or "", "SUSPENDED"
            )
            for _ in range(3 if fault == "still_running" else 1):
                stub.add_response(
                    "get_microvm", {**vm, "state": state}, {"microvmIdentifier": "original"}
                )
            if fault not in {"not_suspended", "still_running", "no_new_reply"}:
                stub.add_response(
                    "get_microvm", {**vm, "state": "RUNNING"}, {"microvmIdentifier": "original"}
                )
                ids = ["original", "replacement"] if fault == "replacement" else ["original"]
                stub.add_response(
                    "list_microvms",
                    {
                        "items": [
                            {**image, "microvmId": vm_id, "state": "RUNNING"} for vm_id in ids
                        ]
                    },
                    {},
                )
            stub.add_response(
                "get_microvm", {**vm, "state": "TERMINATED"}, {"microvmIdentifier": "original"}
            )
            stub.add_response("list_microvms", {"items": []}, {})
            if fault == "marker_lost":
                with pytest.raises(httpx.HTTPStatusError, match="404"):
                    driver.main()
            elif fault:
                with pytest.raises(
                    SystemExit,
                    match=r"expected SUSPENDED|replaced|follow-up turn did not complete",
                ):
                    driver.main()
            else:
                assert driver.main() == 0
            stub.assert_no_pending_responses()
    assert deleted
    assert marker.startswith("/root/workspace/.microvm-resume-")
    assert followed_up is (fault not in {"not_suspended", "still_running"})
    if fault in {"still_running", "no_new_reply"}:
        assert clock >= 5


@pytest.mark.parametrize("skip", ["--skip-aws-check", "--skip-wake"])
def test_suspend_validation_rejects_skipped_checks(
    monkeypatch: pytest.MonkeyPatch, skip: str
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["driver", "--server", "https://server.test", "--suspend", skip]
    )
    with pytest.raises(SystemExit) as caught:
        driver.main()
    assert caught.value.code == 2
