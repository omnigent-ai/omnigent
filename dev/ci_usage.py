"""Prepare CI model attribution and send a bounded gateway verification request."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request

from omnigent.databricks_ai_gateway import (
    REQUEST_TAGS_HEADER,
    ci_gateway_base_url,
    ci_request_tags,
    gateway_request_tag_headers,
)


def ocr_request_headers() -> str:
    """Encode JSON for OCR's quoted, comma-separated extra-header input."""
    encoded = {
        key: json.dumps(value).replace('\\"', "\\u0022").replace(",", "\\u002c")
        for key, value in ci_request_tags().items()
    }
    tags = "{" + ",".join(json.dumps(key) + ":" + value for key, value in encoded.items()) + "}"
    return f'{REQUEST_TAGS_HEADER}="{tags}"'


def probe(protocol: str, model: str) -> None:
    """Send one short request and print its request ID and attribution, never credentials."""
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
    parser.add_argument("command", choices=["ocr-headers", "probe"])
    parser.add_argument("--protocol", choices=["anthropic", "codex/v1"])
    parser.add_argument("--model")
    args = parser.parse_args()
    if not ci_request_tags():
        parser.error("OMNIGENT_CI_PIPELINE or OMNIGENT_CI_CONTEXT must be set")
    if args.command == "ocr-headers":
        print(ocr_request_headers())
    elif not args.protocol or not args.model:
        parser.error("probe requires --protocol and --model")
    else:
        probe(args.protocol, args.model)


if __name__ == "__main__":
    main()
