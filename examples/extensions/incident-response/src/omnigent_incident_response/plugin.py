"""Manifest discovery imports no runtime implementation."""

from omnigent.extensions import (
    EXTENSION_API_VERSION,
    AgentContribution,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    PolicyContribution,
)


def get_manifest() -> ExtensionManifest:
    """Declare the team's portable assets and explicitly activated observer."""
    return ExtensionManifest(
        id="acme.incident-response",
        display_name="Incident response",
        owner="team-reliability",
        distribution="omnigent-incident-response",
        version="0.1.0",
        requires_omnigent=">=0.19,<1",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(server="omnigent_incident_response.hooks:activate"),
        permissions=frozenset({ExtensionPermission.EVENTS_SUBSCRIBE}),
        agents=(
            AgentContribution(
                id="acme.incident-response.triage",
                title="Incident triage",
                path="agents/triage",
                policies=("acme.incident-response.approvals",),
            ),
        ),
        policies=(
            PolicyContribution(
                id="acme.incident-response.approvals",
                title="Approve operating-system tools",
                path="policies/approvals.yaml",
            ),
        ),
    )
