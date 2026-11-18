"""Prepare CI model attribution and send a bounded gateway verification request."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from collections.abc import Mapping

from omnigent.databricks_ai_gateway import (
    REQUEST_TAGS_HEADER,
    ci_gateway_base_url,
    ci_request_tags,
    gateway_request_tag_headers,
)


def chat_request(
    base_url: str, api_key: str, payload: Mapping[str, object]
) -> urllib.request.Request:
    """Build a tagged CI chat request, including multimodal chat responses."""
    if not ci_request_tags(strict=True).get("pipeline"):
        raise ValueError("CI model requests require a pipeline")
    gateway = ci_gateway_base_url(base_url, "mlflow/v1")
    return urllib.request.Request(
        gateway + "/chat/completions",
        data=json.dumps(dict(payload)).encode(),
        headers={
            **gateway_request_tag_headers(gateway),
            "Authorization": "Bearer " + api_key.strip(),
            "Content-Type": "application/json",
        },
        method="POST",
    )


def ocr_request_headers() -> str:
    """Encode JSON for OCR's quoted, comma-separated extra-header input."""
    tags = ci_request_tags(strict=True)
    if not tags.get("pipeline"):
        raise ValueError("CI model requests require a pipeline")
    encoded = {
        key: '"' + json.dumps(value)[1:-1].replace('\\"', "\\u0022").replace(",", "\\u002c") + '"'
        for key, value in tags.items()
    }
    tags = "{" + ",".join(json.dumps(key) + ":" + value for key, value in encoded.items()) + "}"
    return f'{REQUEST_TAGS_HEADER}="{tags}"'


def probe(protocol: str, model: str) -> None:
    """Send one short request and print its request ID and attribution, never credentials."""
    if not ci_request_tags(strict=True).get("pipeline"):
        raise ValueError("CI model requests require a pipeline")
    base_url = ci_gateway_base_url(os.environ["GATEWAY_BASE_URL"], protocol)
    headers = {
        **gateway_request_tag_headers(base_url),
        "Authorization": "Bearer " + os.environ["LLM_API_KEY"],
        "Content-Type": "application/json",
    }
    if protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        # Exercise exactly the header value passed to the OCR action.
        headers[REQUEST_TAGS_HEADER] = ocr_request_headers().split("=", 1)[1][1:-1]
        path = "/v1/messages"
        payload = {
            "model": model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Reply only with OK."}],
        }
    elif protocol == "mlflow/v1":
        path = "/chat/completions"
        payload = {
            "model": model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Reply only with OK."}],
        }
    else:
        path = "/responses"
        payload = {"model": model, "max_output_tokens": 256, "input": "Reply only with OK."}
    request = urllib.request.Request(
        base_url + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.load(response)
        print(
            json.dumps(
                {
                    "protocol": protocol,
                    "request_id": response.headers.get("x-request-id"),
                    "response_id": result.get("id"),
                    "request_tags": ci_request_tags(),
                    "usage": result.get("usage"),
                }
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "probe"])
    parser.add_argument("--protocol", choices=["anthropic", "codex/v1", "mlflow/v1"])
    parser.add_argument("--model")
    args = parser.parse_args()
    if not ci_request_tags(strict=True).get("pipeline"):
        parser.error("OMNIGENT_CI_PIPELINE or OMNIGENT_CI_CONTEXT must be set")
    if args.command == "validate":
        return
    if not args.protocol or not args.model:
        parser.error("probe requires --protocol and --model")
    else:
        probe(args.protocol, args.model)


if __name__ == "__main__":
    main()
