from __future__ import annotations

import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole, QueryEndpointResponse

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.classification import PromptClassifier


def serving_endpoint_classifier(
    endpoint: str,
    areas: AreaCatalog,
    workspace: WorkspaceClient | None = None,
    duplicate_candidates: tuple[dict[str, object], ...] = (),
    *,
    review_bugs: bool = False,
) -> PromptClassifier:
    if not endpoint:
        raise ValueError("model_endpoint is required when issue classifications are missing")
    workspace = workspace or WorkspaceClient()
    gateway_url = None
    gateway_headers = {}
    if os.environ.get("OMNIGENT_CI_PIPELINE") or os.environ.get("OMNIGENT_CI_CONTEXT"):
        # GitHub supplies the trusted checkout on PYTHONPATH; deployed jobs stay standalone.
        from omnigent.databricks_ai_gateway import (
            ci_gateway_base_url,
            ci_request_tags,
            gateway_request_tag_headers,
        )

        if not ci_request_tags(strict=True).get("pipeline"):
            raise ValueError("CI classification requires a pipeline")
        gateway_url = ci_gateway_base_url(workspace.config.host, "mlflow/v1") + "/chat/completions"
        gateway_headers = gateway_request_tag_headers(gateway_url)

    def query(prompt: str) -> str:
        max_tokens = 8192 if review_bugs else 2048
        if gateway_url:
            response = QueryEndpointResponse.from_dict(
                workspace.api_client.do(
                    "POST",
                    url=gateway_url,
                    headers={"Content-Type": "application/json", **gateway_headers},
                    body={
                        "model": endpoint,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                    },
                )
            )
        else:
            response = workspace.serving_endpoints.query(
                endpoint,
                messages=[ChatMessage(role=ChatMessageRole.USER, content=prompt)],
                max_tokens=max_tokens,
            )
        if not response.choices:
            raise RuntimeError("model endpoint returned no choices")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                "model endpoint truncated the classification at its output token limit"
            )
        if choice.message and choice.message.content:
            return choice.message.content
        if choice.text:
            return choice.text
        raise RuntimeError("model endpoint returned an empty response")

    return PromptClassifier(query, areas, duplicate_candidates, review_bugs=review_bugs)
