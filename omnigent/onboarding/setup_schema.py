"""Typed, secret-safe boundary for host-owned setup operations."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from omnigent.harness_availability import HarnessAvailability

Family = Literal["anthropic", "openai", "gemini"]
Surface = Literal["anthropic", "openai", "gemini", "pi"]
HarnessKey = Literal["cursor", "antigravity", "copilot"]
StatusHarness = Literal[
    "claude-native",
    "codex-native",
    "cursor",
    "cursor-native",
    "opencode",
    "opencode-native",
    "pi-native",
    "antigravity",
    "antigravity-native",
    "copilot",
    "qwen",
    "goose",
    "hermes",
    "kiro",
    "kimi",
]
Name = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
]
Identifier = Annotated[str, Field(min_length=1, max_length=4096)]
ModelId = Annotated[str, Field(min_length=1, max_length=512)]


class SetupModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CredentialInput(SetupModel):
    secret: SecretStr | None = None
    env_var: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None = None


class AddKey(CredentialInput):
    action: Literal["add_key"]
    provider: Name
    name: Name | None = None
    model: ModelId | None = None


class AddGateway(CredentialInput):
    action: Literal["add_gateway"]
    name: Name
    base_url: str = Field(min_length=1, max_length=2048)
    families: list[Literal["anthropic", "openai"]] = Field(min_length=1, max_length=2)
    wire_api: Literal["chat", "responses"] = "responses"
    models: dict[Family, ModelId] = Field(default_factory=dict)


class AddBedrock(CredentialInput):
    action: Literal["add_bedrock"]
    name: Name = "bedrock"
    base_url: str = "https://bedrock-runtime.us-east-1.amazonaws.com"
    model: ModelId


class Subscription(SetupModel):
    action: Literal["subscription"]
    cli: Literal["claude", "codex", "pi"]


class AdoptDetected(SetupModel):
    action: Literal["adopt_detected"]
    name: Identifier


class SetDefault(SetupModel):
    action: Literal["set_default"]
    name: Identifier
    surface: Surface


class RemoveProvider(SetupModel):
    action: Literal["remove_provider"]
    name: Identifier


class DismissDetection(SetupModel):
    action: Literal["dismiss_detection"]
    name: Identifier
    dismissed: bool


class SetHarnessKey(CredentialInput):
    action: Literal["set_harness_key"]
    harness: HarnessKey


class RemoveHarnessKey(SetupModel):
    action: Literal["remove_harness_key"]
    harness: HarnessKey


class SetCopilotHost(SetupModel):
    action: Literal["set_copilot_host"]
    host: str | None


class SetOpencodeModel(SetupModel):
    action: Literal["set_opencode_model"]
    model: ModelId | None


class AddAcp(SetupModel):
    action: Literal["add_acp"]
    name: Name
    command: str = Field(min_length=1, max_length=4096)
    model: ModelId | None = None


class RemoveAcp(SetupModel):
    action: Literal["remove_acp"]
    slug: Name


class SetupDetectRequest(SetupModel):
    import_path: str | None = Field(default=None, max_length=4096)
    import_source: Literal["openclaw", "acpx"] | None = None
    harness: StatusHarness | None = None
    pi_default: bool = False


class ImportAcp(SetupModel):
    action: Literal["import_acp"]
    source: Literal["openclaw", "acpx"]
    names: list[Identifier] = Field(min_length=1, max_length=100)
    path: str | None = Field(default=None, max_length=4096)
    fingerprints: dict[str, str] = Field(default_factory=dict)


SetupAction = Annotated[
    AddKey
    | AddGateway
    | AddBedrock
    | Subscription
    | AdoptDetected
    | SetDefault
    | RemoveProvider
    | DismissDetection
    | SetHarnessKey
    | RemoveHarnessKey
    | SetCopilotHost
    | SetOpencodeModel
    | AddAcp
    | RemoveAcp
    | ImportAcp,
    Field(discriminator="action"),
]
SETUP_ACTION_ADAPTER = TypeAdapter(SetupAction)


class SetupProvider(SetupModel):
    name: str
    kind: str
    families: list[str]
    defaults: list[str]
    default_scopes: list[str] = Field(default_factory=list)
    credential_sources: dict[str, str] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)
    base_urls: dict[str, str] = Field(default_factory=dict)
    wire_api: str | None = None
    remove_warning: str | None = None


class KeyProvider(SetupModel):
    id: str
    label: str
    family: str
    base_url: str
    wire_api: str | None = None


class AcpAgent(SetupModel):
    slug: str
    name: str
    command: str
    model: str | None = None
    env_passthrough: list[str] = Field(default_factory=list)
    session_id_mode: str = "server"
    send_model: bool = False
    omnigent_mcp: bool = True
    inject_system_prompt: bool = True


class HarnessSettings(SetupModel):
    cursor_key_configured: bool = False
    antigravity_key_configured: bool = False
    copilot_key_configured: bool = False
    copilot_host: str | None = None
    opencode_model: str | None = None


class BuiltinAcpSetup(SetupModel):
    id: str
    label: str
    install_command: str
    auth_instructions: str


class SetupInventory(SetupModel):
    feature_enabled: bool = True
    supported_operations: list[str] = Field(default_factory=list)
    providers: list[SetupProvider] = Field(default_factory=list)
    key_providers: list[KeyProvider] = Field(default_factory=list)
    acp_agents: list[AcpAgent] = Field(default_factory=list)
    builtin_acp: list[BuiltinAcpSetup] = Field(default_factory=list)
    harness_settings: HarnessSettings = Field(default_factory=HarnessSettings)
    dismissed_detections: list[str] = Field(default_factory=list)
    effective_defaults: dict[str, str | None] = Field(default_factory=dict)
    pi_default_requires_detection: bool = False


class DetectedConnection(SetupModel):
    name: str
    kind: str
    family: str
    source: str
    display_name: str | None = None


class ImportPreview(SetupModel):
    source: Literal["openclaw", "acpx"]
    name: str
    slug: str
    command: str
    model: str | None = None
    fingerprint: str


class HarnessStatus(SetupModel):
    harness: StatusHarness
    availability: HarnessAvailability


class SetupDetection(SetupModel):
    providers: list[DetectedConnection] = Field(default_factory=list)
    imports: list[ImportPreview] = Field(default_factory=list)
    models: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    default_models: dict[str, str | None] = Field(default_factory=dict)
    harness_status: HarnessStatus | None = None
    pi_default_provider: str | None = None
    pi_default_checked: bool = False


class SetupActionResult(SetupModel):
    ok: bool = True
    message: str
    inventory: SetupInventory
