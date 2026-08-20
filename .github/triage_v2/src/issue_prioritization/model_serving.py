from __future__ import annotations

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.classification import PromptClassifier


def serving_endpoint_classifier(
    endpoint: str,
    areas: AreaCatalog,
    workspace: WorkspaceClient | None = None,
) -> PromptClassifier:
    if not endpoint:
        raise ValueError("model_endpoint is required when issue classifications are missing")
    workspace = workspace or WorkspaceClient()

    def query(prompt: str) -> str:
        response = workspace.serving_endpoints.query(
            endpoint,
            messages=[ChatMessage(role=ChatMessageRole.USER, content=prompt)],
            max_tokens=2048,
        )
        if not response.choices:
            raise RuntimeError("model endpoint returned no choices")
        choice = response.choices[0]
        if choice.message and choice.message.content:
            return choice.message.content
        if choice.text:
            return choice.text
        raise RuntimeError("model endpoint returned an empty response")

    return PromptClassifier(query, areas)
