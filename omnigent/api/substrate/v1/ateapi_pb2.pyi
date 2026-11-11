import datetime

from google.protobuf import descriptor_pb2 as _descriptor_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SnapshotOwner(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SNAPSHOT_OWNER_UNSPECIFIED: _ClassVar[SnapshotOwner]
    SNAPSHOT_OWNER_ACTOR: _ClassVar[SnapshotOwner]
    SNAPSHOT_OWNER_TAG: _ClassVar[SnapshotOwner]

class SnapshotStorageState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SNAPSHOT_STORAGE_STATE_UNSPECIFIED: _ClassVar[SnapshotStorageState]
    SNAPSHOT_STORAGE_STATE_PENDING: _ClassVar[SnapshotStorageState]
    SNAPSHOT_STORAGE_STATE_COMPLETED: _ClassVar[SnapshotStorageState]

class SnapshotFidelity(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SNAPSHOT_FIDELITY_UNSPECIFIED: _ClassVar[SnapshotFidelity]
    SNAPSHOT_FIDELITY_VOLUMES: _ClassVar[SnapshotFidelity]
    SNAPSHOT_FIDELITY_ROOTFS: _ClassVar[SnapshotFidelity]
    SNAPSHOT_FIDELITY_MEMORY: _ClassVar[SnapshotFidelity]

class TagScope(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TAG_SCOPE_UNSPECIFIED: _ClassVar[TagScope]
    TAG_SCOPE_ATESPACE: _ClassVar[TagScope]
    TAG_SCOPE_PUBLISHED: _ClassVar[TagScope]

class ActorState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ACTOR_STATE_UNSPECIFIED: _ClassVar[ActorState]
    ACTOR_STATE_RESUMING: _ClassVar[ActorState]
    ACTOR_STATE_RUNNING: _ClassVar[ActorState]
    ACTOR_STATE_SUSPENDING: _ClassVar[ActorState]
    ACTOR_STATE_SUSPENDED: _ClassVar[ActorState]
    ACTOR_STATE_CRASHED: _ClassVar[ActorState]
    ACTOR_STATE_DELETING: _ClassVar[ActorState]
    ACTOR_STATE_REVERTING: _ClassVar[ActorState]

class GoldenSnapshotMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    GOLDEN_SNAPSHOT_MODE_UNSPECIFIED: _ClassVar[GoldenSnapshotMode]
    GOLDEN_SNAPSHOT_MODE_ENABLED: _ClassVar[GoldenSnapshotMode]
    GOLDEN_SNAPSHOT_MODE_DISABLED: _ClassVar[GoldenSnapshotMode]

class ActorMetadataField(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ACTOR_METADATA_FIELD_UNSPECIFIED: _ClassVar[ActorMetadataField]
    ACTOR_METADATA_FIELD_NAME: _ClassVar[ActorMetadataField]
    ACTOR_METADATA_FIELD_ATESPACE: _ClassVar[ActorMetadataField]
    ACTOR_METADATA_FIELD_UID: _ClassVar[ActorMetadataField]

class DurabilityMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DURABILITY_MODE_UNSPECIFIED: _ClassVar[DurabilityMode]
    DURABILITY_MODE_SYNCHRONOUS: _ClassVar[DurabilityMode]
    DURABILITY_MODE_ASYNCHRONOUS: _ClassVar[DurabilityMode]

class WorkerState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    WORKER_STATE_UNSPECIFIED: _ClassVar[WorkerState]
    WORKER_STATE_ACTIVE: _ClassVar[WorkerState]
    WORKER_STATE_DRAINING: _ClassVar[WorkerState]
    WORKER_STATE_UNAVAILABLE: _ClassVar[WorkerState]

class Scope(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SCOPE_UNSPECIFIED: _ClassVar[Scope]
    SCOPE_ATESPACE: _ClassVar[Scope]
    SCOPE_GLOBAL: _ClassVar[Scope]
SNAPSHOT_OWNER_UNSPECIFIED: SnapshotOwner
SNAPSHOT_OWNER_ACTOR: SnapshotOwner
SNAPSHOT_OWNER_TAG: SnapshotOwner
SNAPSHOT_STORAGE_STATE_UNSPECIFIED: SnapshotStorageState
SNAPSHOT_STORAGE_STATE_PENDING: SnapshotStorageState
SNAPSHOT_STORAGE_STATE_COMPLETED: SnapshotStorageState
SNAPSHOT_FIDELITY_UNSPECIFIED: SnapshotFidelity
SNAPSHOT_FIDELITY_VOLUMES: SnapshotFidelity
SNAPSHOT_FIDELITY_ROOTFS: SnapshotFidelity
SNAPSHOT_FIDELITY_MEMORY: SnapshotFidelity
TAG_SCOPE_UNSPECIFIED: TagScope
TAG_SCOPE_ATESPACE: TagScope
TAG_SCOPE_PUBLISHED: TagScope
ACTOR_STATE_UNSPECIFIED: ActorState
ACTOR_STATE_RESUMING: ActorState
ACTOR_STATE_RUNNING: ActorState
ACTOR_STATE_SUSPENDING: ActorState
ACTOR_STATE_SUSPENDED: ActorState
ACTOR_STATE_CRASHED: ActorState
ACTOR_STATE_DELETING: ActorState
ACTOR_STATE_REVERTING: ActorState
GOLDEN_SNAPSHOT_MODE_UNSPECIFIED: GoldenSnapshotMode
GOLDEN_SNAPSHOT_MODE_ENABLED: GoldenSnapshotMode
GOLDEN_SNAPSHOT_MODE_DISABLED: GoldenSnapshotMode
ACTOR_METADATA_FIELD_UNSPECIFIED: ActorMetadataField
ACTOR_METADATA_FIELD_NAME: ActorMetadataField
ACTOR_METADATA_FIELD_ATESPACE: ActorMetadataField
ACTOR_METADATA_FIELD_UID: ActorMetadataField
DURABILITY_MODE_UNSPECIFIED: DurabilityMode
DURABILITY_MODE_SYNCHRONOUS: DurabilityMode
DURABILITY_MODE_ASYNCHRONOUS: DurabilityMode
WORKER_STATE_UNSPECIFIED: WorkerState
WORKER_STATE_ACTIVE: WorkerState
WORKER_STATE_DRAINING: WorkerState
WORKER_STATE_UNAVAILABLE: WorkerState
SCOPE_UNSPECIFIED: Scope
SCOPE_ATESPACE: Scope
SCOPE_GLOBAL: Scope
RESOURCE_FIELD_NUMBER: int
resource: _descriptor.FieldDescriptor
METHOD_FIELD_NUMBER: int
method: _descriptor.FieldDescriptor

class ObjectSnapshot(_message.Message):
    __slots__ = ("snapshot_uri",)
    SNAPSHOT_URI_FIELD_NUMBER: _ClassVar[int]
    snapshot_uri: str
    def __init__(self, snapshot_uri: _Optional[str] = ...) -> None: ...

class SnapshotStorage(_message.Message):
    __slots__ = ("state", "object")
    STATE_FIELD_NUMBER: _ClassVar[int]
    OBJECT_FIELD_NUMBER: _ClassVar[int]
    state: SnapshotStorageState
    object: ObjectSnapshot
    def __init__(self, state: _Optional[_Union[SnapshotStorageState, str]] = ..., object: _Optional[_Union[ObjectSnapshot, _Mapping]] = ...) -> None: ...

class Snapshot(_message.Message):
    __slots__ = ("uuid", "generation", "owner", "fidelity", "actor_template_uid", "locality", "durable_snapshot", "sandbox_runtime")
    UUID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    FIDELITY_FIELD_NUMBER: _ClassVar[int]
    ACTOR_TEMPLATE_UID_FIELD_NUMBER: _ClassVar[int]
    LOCALITY_FIELD_NUMBER: _ClassVar[int]
    DURABLE_SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_RUNTIME_FIELD_NUMBER: _ClassVar[int]
    uuid: str
    generation: int
    owner: SnapshotOwner
    fidelity: SnapshotFidelity
    actor_template_uid: str
    locality: str
    durable_snapshot: SnapshotStorage
    sandbox_runtime: ActorSandboxRuntime
    def __init__(self, uuid: _Optional[str] = ..., generation: _Optional[int] = ..., owner: _Optional[_Union[SnapshotOwner, str]] = ..., fidelity: _Optional[_Union[SnapshotFidelity, str]] = ..., actor_template_uid: _Optional[str] = ..., locality: _Optional[str] = ..., durable_snapshot: _Optional[_Union[SnapshotStorage, _Mapping]] = ..., sandbox_runtime: _Optional[_Union[ActorSandboxRuntime, _Mapping]] = ...) -> None: ...

class Selector(_message.Message):
    __slots__ = ("match_labels",)
    class MatchLabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    MATCH_LABELS_FIELD_NUMBER: _ClassVar[int]
    match_labels: _containers.ScalarMap[str, str]
    def __init__(self, match_labels: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ResourceMetadata(_message.Message):
    __slots__ = ("atespace", "name", "uid", "version", "create_time", "update_time", "delete_time")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    UID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    CREATE_TIME_FIELD_NUMBER: _ClassVar[int]
    UPDATE_TIME_FIELD_NUMBER: _ClassVar[int]
    DELETE_TIME_FIELD_NUMBER: _ClassVar[int]
    atespace: str
    name: str
    uid: str
    version: int
    create_time: _timestamp_pb2.Timestamp
    update_time: _timestamp_pb2.Timestamp
    delete_time: _timestamp_pb2.Timestamp
    def __init__(self, atespace: _Optional[str] = ..., name: _Optional[str] = ..., uid: _Optional[str] = ..., version: _Optional[int] = ..., create_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., update_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., delete_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ExternalVolumes(_message.Message):
    __slots__ = ("volumes",)
    VOLUMES_FIELD_NUMBER: _ClassVar[int]
    volumes: _containers.RepeatedCompositeFieldContainer[ExternalVolumeStatus]
    def __init__(self, volumes: _Optional[_Iterable[_Union[ExternalVolumeStatus, _Mapping]]] = ...) -> None: ...

class ExternalVolumeStatus(_message.Message):
    __slots__ = ("name", "storage_volume_id", "volume_type", "volume_context")
    class VolumeContextEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    NAME_FIELD_NUMBER: _ClassVar[int]
    STORAGE_VOLUME_ID_FIELD_NUMBER: _ClassVar[int]
    VOLUME_TYPE_FIELD_NUMBER: _ClassVar[int]
    VOLUME_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    name: str
    storage_volume_id: str
    volume_type: str
    volume_context: _containers.ScalarMap[str, str]
    def __init__(self, name: _Optional[str] = ..., storage_volume_id: _Optional[str] = ..., volume_type: _Optional[str] = ..., volume_context: _Optional[_Mapping[str, str]] = ...) -> None: ...

class Actor(_message.Message):
    __slots__ = ("metadata", "actor_template", "worker_selector", "source_tag", "status")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    ACTOR_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    WORKER_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TAG_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    metadata: ResourceMetadata
    actor_template: ObjectRef
    worker_selector: Selector
    source_tag: ObjectRef
    status: ActorStatus
    def __init__(self, metadata: _Optional[_Union[ResourceMetadata, _Mapping]] = ..., actor_template: _Optional[_Union[ObjectRef, _Mapping]] = ..., worker_selector: _Optional[_Union[Selector, _Mapping]] = ..., source_tag: _Optional[_Union[ObjectRef, _Mapping]] = ..., status: _Optional[_Union[ActorStatus, _Mapping]] = ...) -> None: ...

class EgressPolicy(_message.Message):
    __slots__ = ("metadata", "rules")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    RULES_FIELD_NUMBER: _ClassVar[int]
    metadata: ResourceMetadata
    rules: _containers.RepeatedCompositeFieldContainer[EgressRule]
    def __init__(self, metadata: _Optional[_Union[ResourceMetadata, _Mapping]] = ..., rules: _Optional[_Iterable[_Union[EgressRule, _Mapping]]] = ...) -> None: ...

class EgressPolicyTemplate(_message.Message):
    __slots__ = ("rules",)
    RULES_FIELD_NUMBER: _ClassVar[int]
    rules: _containers.RepeatedCompositeFieldContainer[EgressRule]
    def __init__(self, rules: _Optional[_Iterable[_Union[EgressRule, _Mapping]]] = ...) -> None: ...

class EgressRule(_message.Message):
    __slots__ = ("http", "https", "tls_passthrough")
    HTTP_FIELD_NUMBER: _ClassVar[int]
    HTTPS_FIELD_NUMBER: _ClassVar[int]
    TLS_PASSTHROUGH_FIELD_NUMBER: _ClassVar[int]
    http: HttpRule
    https: HttpsRule
    tls_passthrough: TlsPassthroughRule
    def __init__(self, http: _Optional[_Union[HttpRule, _Mapping]] = ..., https: _Optional[_Union[HttpsRule, _Mapping]] = ..., tls_passthrough: _Optional[_Union[TlsPassthroughRule, _Mapping]] = ...) -> None: ...

class HttpRule(_message.Message):
    __slots__ = ("hostnames", "ports", "effects")
    HOSTNAMES_FIELD_NUMBER: _ClassVar[int]
    PORTS_FIELD_NUMBER: _ClassVar[int]
    EFFECTS_FIELD_NUMBER: _ClassVar[int]
    hostnames: _containers.RepeatedScalarFieldContainer[str]
    ports: Ports
    effects: HttpRuleEffects
    def __init__(self, hostnames: _Optional[_Iterable[str]] = ..., ports: _Optional[_Union[Ports, _Mapping]] = ..., effects: _Optional[_Union[HttpRuleEffects, _Mapping]] = ...) -> None: ...

class HttpsRule(_message.Message):
    __slots__ = ("hostnames", "ports", "effects")
    HOSTNAMES_FIELD_NUMBER: _ClassVar[int]
    PORTS_FIELD_NUMBER: _ClassVar[int]
    EFFECTS_FIELD_NUMBER: _ClassVar[int]
    hostnames: _containers.RepeatedScalarFieldContainer[str]
    ports: Ports
    effects: HttpRuleEffects
    def __init__(self, hostnames: _Optional[_Iterable[str]] = ..., ports: _Optional[_Union[Ports, _Mapping]] = ..., effects: _Optional[_Union[HttpRuleEffects, _Mapping]] = ...) -> None: ...

class TlsPassthroughRule(_message.Message):
    __slots__ = ("hostnames", "ports")
    HOSTNAMES_FIELD_NUMBER: _ClassVar[int]
    PORTS_FIELD_NUMBER: _ClassVar[int]
    hostnames: _containers.RepeatedScalarFieldContainer[str]
    ports: Ports
    def __init__(self, hostnames: _Optional[_Iterable[str]] = ..., ports: _Optional[_Union[Ports, _Mapping]] = ...) -> None: ...

class Ports(_message.Message):
    __slots__ = ("all", "numbers")
    ALL_FIELD_NUMBER: _ClassVar[int]
    NUMBERS_FIELD_NUMBER: _ClassVar[int]
    all: AllPorts
    numbers: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, all: _Optional[_Union[AllPorts, _Mapping]] = ..., numbers: _Optional[_Iterable[int]] = ...) -> None: ...

class AllPorts(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HttpRuleEffects(_message.Message):
    __slots__ = ("replace_headers",)
    REPLACE_HEADERS_FIELD_NUMBER: _ClassVar[int]
    replace_headers: _containers.RepeatedCompositeFieldContainer[CredentialHeader]
    def __init__(self, replace_headers: _Optional[_Iterable[_Union[CredentialHeader, _Mapping]]] = ...) -> None: ...

class CredentialHeader(_message.Message):
    __slots__ = ("header", "prefix", "credential_uri", "actor_jwt")
    HEADER_FIELD_NUMBER: _ClassVar[int]
    PREFIX_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_URI_FIELD_NUMBER: _ClassVar[int]
    ACTOR_JWT_FIELD_NUMBER: _ClassVar[int]
    header: str
    prefix: str
    credential_uri: str
    actor_jwt: ActorJwtSource
    def __init__(self, header: _Optional[str] = ..., prefix: _Optional[str] = ..., credential_uri: _Optional[str] = ..., actor_jwt: _Optional[_Union[ActorJwtSource, _Mapping]] = ...) -> None: ...

class ActorJwtSource(_message.Message):
    __slots__ = ("audiences", "expiration_seconds")
    AUDIENCES_FIELD_NUMBER: _ClassVar[int]
    EXPIRATION_SECONDS_FIELD_NUMBER: _ClassVar[int]
    audiences: _containers.RepeatedScalarFieldContainer[str]
    expiration_seconds: int
    def __init__(self, audiences: _Optional[_Iterable[str]] = ..., expiration_seconds: _Optional[int] = ...) -> None: ...

class ActorStatus(_message.Message):
    __slots__ = ("state", "worker_assignment", "external_volumes", "crash", "assigned_node", "snapshots", "last_assigned_generation")
    STATE_FIELD_NUMBER: _ClassVar[int]
    WORKER_ASSIGNMENT_FIELD_NUMBER: _ClassVar[int]
    EXTERNAL_VOLUMES_FIELD_NUMBER: _ClassVar[int]
    CRASH_FIELD_NUMBER: _ClassVar[int]
    ASSIGNED_NODE_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOTS_FIELD_NUMBER: _ClassVar[int]
    LAST_ASSIGNED_GENERATION_FIELD_NUMBER: _ClassVar[int]
    state: ActorState
    worker_assignment: WorkerAssignment
    external_volumes: ExternalVolumes
    crash: ActorCrash
    assigned_node: str
    snapshots: _containers.RepeatedCompositeFieldContainer[Snapshot]
    last_assigned_generation: int
    def __init__(self, state: _Optional[_Union[ActorState, str]] = ..., worker_assignment: _Optional[_Union[WorkerAssignment, _Mapping]] = ..., external_volumes: _Optional[_Union[ExternalVolumes, _Mapping]] = ..., crash: _Optional[_Union[ActorCrash, _Mapping]] = ..., assigned_node: _Optional[str] = ..., snapshots: _Optional[_Iterable[_Union[Snapshot, _Mapping]]] = ..., last_assigned_generation: _Optional[int] = ...) -> None: ...

class ActorCrash(_message.Message):
    __slots__ = ("message", "crash_time")
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    CRASH_TIME_FIELD_NUMBER: _ClassVar[int]
    message: str
    crash_time: _timestamp_pb2.Timestamp
    def __init__(self, message: _Optional[str] = ..., crash_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class WorkerAssignment(_message.Message):
    __slots__ = ("worker", "worker_namespace", "worker_pool", "worker_pod", "worker_pod_uid", "worker_pod_ips", "node_name", "worker_epoch", "sandbox_runtime", "worker_spiffe_id")
    WORKER_FIELD_NUMBER: _ClassVar[int]
    WORKER_NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    WORKER_POOL_FIELD_NUMBER: _ClassVar[int]
    WORKER_POD_FIELD_NUMBER: _ClassVar[int]
    WORKER_POD_UID_FIELD_NUMBER: _ClassVar[int]
    WORKER_POD_IPS_FIELD_NUMBER: _ClassVar[int]
    NODE_NAME_FIELD_NUMBER: _ClassVar[int]
    WORKER_EPOCH_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_RUNTIME_FIELD_NUMBER: _ClassVar[int]
    WORKER_SPIFFE_ID_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    worker_namespace: str
    worker_pool: str
    worker_pod: str
    worker_pod_uid: str
    worker_pod_ips: _containers.RepeatedScalarFieldContainer[str]
    node_name: str
    worker_epoch: int
    sandbox_runtime: ActorSandboxRuntime
    worker_spiffe_id: str
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ..., worker_namespace: _Optional[str] = ..., worker_pool: _Optional[str] = ..., worker_pod: _Optional[str] = ..., worker_pod_uid: _Optional[str] = ..., worker_pod_ips: _Optional[_Iterable[str]] = ..., node_name: _Optional[str] = ..., worker_epoch: _Optional[int] = ..., sandbox_runtime: _Optional[_Union[ActorSandboxRuntime, _Mapping]] = ..., worker_spiffe_id: _Optional[str] = ...) -> None: ...

class TagStatus(_message.Message):
    __slots__ = ("snapshot", "actor_template_uid")
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    ACTOR_TEMPLATE_UID_FIELD_NUMBER: _ClassVar[int]
    snapshot: Snapshot
    actor_template_uid: str
    def __init__(self, snapshot: _Optional[_Union[Snapshot, _Mapping]] = ..., actor_template_uid: _Optional[str] = ...) -> None: ...

class Tag(_message.Message):
    __slots__ = ("metadata", "status", "scope", "source_actor")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ACTOR_FIELD_NUMBER: _ClassVar[int]
    metadata: ResourceMetadata
    status: TagStatus
    scope: TagScope
    source_actor: ObjectRef
    def __init__(self, metadata: _Optional[_Union[ResourceMetadata, _Mapping]] = ..., status: _Optional[_Union[TagStatus, _Mapping]] = ..., scope: _Optional[_Union[TagScope, str]] = ..., source_actor: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class Atespace(_message.Message):
    __slots__ = ("metadata",)
    METADATA_FIELD_NUMBER: _ClassVar[int]
    metadata: ResourceMetadata
    def __init__(self, metadata: _Optional[_Union[ResourceMetadata, _Mapping]] = ...) -> None: ...

class ObjectRef(_message.Message):
    __slots__ = ("atespace", "name")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    atespace: str
    name: str
    def __init__(self, atespace: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class ActorTemplate(_message.Message):
    __slots__ = ("metadata", "worker_selector", "containers", "volumes", "snapshot_config", "resources", "golden_egress_policy", "status")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    WORKER_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    CONTAINERS_FIELD_NUMBER: _ClassVar[int]
    VOLUMES_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_CONFIG_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    GOLDEN_EGRESS_POLICY_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    metadata: ResourceMetadata
    worker_selector: Selector
    containers: _containers.RepeatedCompositeFieldContainer[Container]
    volumes: _containers.RepeatedCompositeFieldContainer[Volume]
    snapshot_config: SnapshotConfig
    resources: ResourceRequirements
    golden_egress_policy: EgressPolicyTemplate
    status: ActorTemplateStatus
    def __init__(self, metadata: _Optional[_Union[ResourceMetadata, _Mapping]] = ..., worker_selector: _Optional[_Union[Selector, _Mapping]] = ..., containers: _Optional[_Iterable[_Union[Container, _Mapping]]] = ..., volumes: _Optional[_Iterable[_Union[Volume, _Mapping]]] = ..., snapshot_config: _Optional[_Union[SnapshotConfig, _Mapping]] = ..., resources: _Optional[_Union[ResourceRequirements, _Mapping]] = ..., golden_egress_policy: _Optional[_Union[EgressPolicyTemplate, _Mapping]] = ..., status: _Optional[_Union[ActorTemplateStatus, _Mapping]] = ...) -> None: ...

class ResourceRequirements(_message.Message):
    __slots__ = ("limits",)
    LIMITS_FIELD_NUMBER: _ClassVar[int]
    limits: Resources
    def __init__(self, limits: _Optional[_Union[Resources, _Mapping]] = ...) -> None: ...

class Resources(_message.Message):
    __slots__ = ("cpu_millicores", "memory_bytes")
    CPU_MILLICORES_FIELD_NUMBER: _ClassVar[int]
    MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    cpu_millicores: int
    memory_bytes: int
    def __init__(self, cpu_millicores: _Optional[int] = ..., memory_bytes: _Optional[int] = ...) -> None: ...

class GoldenSnapshotStatus(_message.Message):
    __slots__ = ("golden_tag", "snapshot_time", "error_message")
    GOLDEN_TAG_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_TIME_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    golden_tag: ObjectRef
    snapshot_time: _timestamp_pb2.Timestamp
    error_message: str
    def __init__(self, golden_tag: _Optional[_Union[ObjectRef, _Mapping]] = ..., snapshot_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., error_message: _Optional[str] = ...) -> None: ...

class ActorTemplateStatus(_message.Message):
    __slots__ = ("golden_snapshot_status",)
    GOLDEN_SNAPSHOT_STATUS_FIELD_NUMBER: _ClassVar[int]
    golden_snapshot_status: GoldenSnapshotStatus
    def __init__(self, golden_snapshot_status: _Optional[_Union[GoldenSnapshotStatus, _Mapping]] = ...) -> None: ...

class SnapshotConfig(_message.Message):
    __slots__ = ("storage_location", "preferred_fidelity", "golden_snapshot_config")
    STORAGE_LOCATION_FIELD_NUMBER: _ClassVar[int]
    PREFERRED_FIDELITY_FIELD_NUMBER: _ClassVar[int]
    GOLDEN_SNAPSHOT_CONFIG_FIELD_NUMBER: _ClassVar[int]
    storage_location: str
    preferred_fidelity: SnapshotFidelity
    golden_snapshot_config: GoldenSnapshotConfig
    def __init__(self, storage_location: _Optional[str] = ..., preferred_fidelity: _Optional[_Union[SnapshotFidelity, str]] = ..., golden_snapshot_config: _Optional[_Union[GoldenSnapshotConfig, _Mapping]] = ...) -> None: ...

class GoldenSnapshotConfig(_message.Message):
    __slots__ = ("mode",)
    MODE_FIELD_NUMBER: _ClassVar[int]
    mode: GoldenSnapshotMode
    def __init__(self, mode: _Optional[_Union[GoldenSnapshotMode, str]] = ...) -> None: ...

class Container(_message.Message):
    __slots__ = ("name", "image", "command", "args", "env", "wakeup_probe", "volume_mounts", "security_context", "resources")
    NAME_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    WAKEUP_PROBE_FIELD_NUMBER: _ClassVar[int]
    VOLUME_MOUNTS_FIELD_NUMBER: _ClassVar[int]
    SECURITY_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    name: str
    image: str
    command: _containers.RepeatedScalarFieldContainer[str]
    args: _containers.RepeatedScalarFieldContainer[str]
    env: _containers.RepeatedCompositeFieldContainer[EnvVar]
    wakeup_probe: WakeupProbe
    volume_mounts: _containers.RepeatedCompositeFieldContainer[VolumeMount]
    security_context: SecurityContext
    resources: ResourceRequirements
    def __init__(self, name: _Optional[str] = ..., image: _Optional[str] = ..., command: _Optional[_Iterable[str]] = ..., args: _Optional[_Iterable[str]] = ..., env: _Optional[_Iterable[_Union[EnvVar, _Mapping]]] = ..., wakeup_probe: _Optional[_Union[WakeupProbe, _Mapping]] = ..., volume_mounts: _Optional[_Iterable[_Union[VolumeMount, _Mapping]]] = ..., security_context: _Optional[_Union[SecurityContext, _Mapping]] = ..., resources: _Optional[_Union[ResourceRequirements, _Mapping]] = ...) -> None: ...

class SecurityContext(_message.Message):
    __slots__ = ("capabilities",)
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    capabilities: Capabilities
    def __init__(self, capabilities: _Optional[_Union[Capabilities, _Mapping]] = ...) -> None: ...

class Capabilities(_message.Message):
    __slots__ = ("add", "drop")
    ADD_FIELD_NUMBER: _ClassVar[int]
    DROP_FIELD_NUMBER: _ClassVar[int]
    add: _containers.RepeatedScalarFieldContainer[str]
    drop: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, add: _Optional[_Iterable[str]] = ..., drop: _Optional[_Iterable[str]] = ...) -> None: ...

class EnvVar(_message.Message):
    __slots__ = ("name", "value")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    def __init__(self, name: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class WakeupProbe(_message.Message):
    __slots__ = ("http_get", "timeout_seconds")
    HTTP_GET_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    http_get: HttpGetAction
    timeout_seconds: int
    def __init__(self, http_get: _Optional[_Union[HttpGetAction, _Mapping]] = ..., timeout_seconds: _Optional[int] = ...) -> None: ...

class HttpGetAction(_message.Message):
    __slots__ = ("path", "port")
    PATH_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    path: str
    port: int
    def __init__(self, path: _Optional[str] = ..., port: _Optional[int] = ...) -> None: ...

class Volume(_message.Message):
    __slots__ = ("name", "durable_dir", "external", "system_info", "image")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DURABLE_DIR_FIELD_NUMBER: _ClassVar[int]
    EXTERNAL_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_INFO_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    name: str
    durable_dir: DurableDirVolumeSource
    external: ExternalVolumeSource
    system_info: SystemInfoVolumeSource
    image: ImageVolumeSource
    def __init__(self, name: _Optional[str] = ..., durable_dir: _Optional[_Union[DurableDirVolumeSource, _Mapping]] = ..., external: _Optional[_Union[ExternalVolumeSource, _Mapping]] = ..., system_info: _Optional[_Union[SystemInfoVolumeSource, _Mapping]] = ..., image: _Optional[_Union[ImageVolumeSource, _Mapping]] = ...) -> None: ...

class ExternalVolumeSource(_message.Message):
    __slots__ = ("template",)
    TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    template: ExternalVolumeTemplate
    def __init__(self, template: _Optional[_Union[ExternalVolumeTemplate, _Mapping]] = ...) -> None: ...

class ImageVolumeSource(_message.Message):
    __slots__ = ("reference",)
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    reference: str
    def __init__(self, reference: _Optional[str] = ...) -> None: ...

class DurableDirVolumeSource(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ExternalVolumeTemplate(_message.Message):
    __slots__ = ("capacity_bytes", "storage_class_name")
    CAPACITY_BYTES_FIELD_NUMBER: _ClassVar[int]
    STORAGE_CLASS_NAME_FIELD_NUMBER: _ClassVar[int]
    capacity_bytes: int
    storage_class_name: str
    def __init__(self, capacity_bytes: _Optional[int] = ..., storage_class_name: _Optional[str] = ...) -> None: ...

class SystemInfoVolumeSource(_message.Message):
    __slots__ = ("data_sources",)
    DATA_SOURCES_FIELD_NUMBER: _ClassVar[int]
    data_sources: _containers.RepeatedCompositeFieldContainer[SystemInfoDataSource]
    def __init__(self, data_sources: _Optional[_Iterable[_Union[SystemInfoDataSource, _Mapping]]] = ...) -> None: ...

class SystemInfoDataSource(_message.Message):
    __slots__ = ("actor_metadata", "trust_bundle")
    ACTOR_METADATA_FIELD_NUMBER: _ClassVar[int]
    TRUST_BUNDLE_FIELD_NUMBER: _ClassVar[int]
    actor_metadata: ActorMetadataDataSource
    trust_bundle: TrustBundleDataSource
    def __init__(self, actor_metadata: _Optional[_Union[ActorMetadataDataSource, _Mapping]] = ..., trust_bundle: _Optional[_Union[TrustBundleDataSource, _Mapping]] = ...) -> None: ...

class ActorMetadataDataSource(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[ActorMetadataItem]
    def __init__(self, items: _Optional[_Iterable[_Union[ActorMetadataItem, _Mapping]]] = ...) -> None: ...

class ActorMetadataItem(_message.Message):
    __slots__ = ("field", "path")
    FIELD_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    field: ActorMetadataField
    path: str
    def __init__(self, field: _Optional[_Union[ActorMetadataField, str]] = ..., path: _Optional[str] = ...) -> None: ...

class TrustBundleDataSource(_message.Message):
    __slots__ = ("names", "path")
    NAMES_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    names: _containers.RepeatedScalarFieldContainer[str]
    path: str
    def __init__(self, names: _Optional[_Iterable[str]] = ..., path: _Optional[str] = ...) -> None: ...

class VolumeMount(_message.Message):
    __slots__ = ("name", "mount_path")
    NAME_FIELD_NUMBER: _ClassVar[int]
    MOUNT_PATH_FIELD_NUMBER: _ClassVar[int]
    name: str
    mount_path: str
    def __init__(self, name: _Optional[str] = ..., mount_path: _Optional[str] = ...) -> None: ...

class CreateAtespaceRequest(_message.Message):
    __slots__ = ("atespace",)
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    atespace: Atespace
    def __init__(self, atespace: _Optional[_Union[Atespace, _Mapping]] = ...) -> None: ...

class GetAtespaceRequest(_message.Message):
    __slots__ = ("atespace",)
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    atespace: ObjectRef
    def __init__(self, atespace: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class ListAtespacesRequest(_message.Message):
    __slots__ = ("options",)
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    options: ListOptions
    def __init__(self, options: _Optional[_Union[ListOptions, _Mapping]] = ...) -> None: ...

class ListAtespacesResponse(_message.Message):
    __slots__ = ("atespaces", "next_page_token")
    ATESPACES_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    atespaces: _containers.RepeatedCompositeFieldContainer[Atespace]
    next_page_token: str
    def __init__(self, atespaces: _Optional[_Iterable[_Union[Atespace, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class DeleteAtespaceRequest(_message.Message):
    __slots__ = ("atespace", "options")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    atespace: ObjectRef
    options: DeleteOptions
    def __init__(self, atespace: _Optional[_Union[ObjectRef, _Mapping]] = ..., options: _Optional[_Union[DeleteOptions, _Mapping]] = ...) -> None: ...

class CreateActorTemplateRequest(_message.Message):
    __slots__ = ("actor_template",)
    ACTOR_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    actor_template: ActorTemplate
    def __init__(self, actor_template: _Optional[_Union[ActorTemplate, _Mapping]] = ...) -> None: ...

class GetActorTemplateRequest(_message.Message):
    __slots__ = ("actor_template",)
    ACTOR_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    actor_template: ObjectRef
    def __init__(self, actor_template: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class ListActorTemplatesRequest(_message.Message):
    __slots__ = ("atespace", "options")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    atespace: str
    options: ListOptions
    def __init__(self, atespace: _Optional[str] = ..., options: _Optional[_Union[ListOptions, _Mapping]] = ...) -> None: ...

class ListActorTemplatesResponse(_message.Message):
    __slots__ = ("actor_templates", "next_page_token")
    ACTOR_TEMPLATES_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    actor_templates: _containers.RepeatedCompositeFieldContainer[ActorTemplate]
    next_page_token: str
    def __init__(self, actor_templates: _Optional[_Iterable[_Union[ActorTemplate, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class DeleteActorTemplateRequest(_message.Message):
    __slots__ = ("actor_template", "options")
    ACTOR_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    actor_template: ObjectRef
    options: DeleteOptions
    def __init__(self, actor_template: _Optional[_Union[ObjectRef, _Mapping]] = ..., options: _Optional[_Union[DeleteOptions, _Mapping]] = ...) -> None: ...

class GetActorRequest(_message.Message):
    __slots__ = ("actor",)
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class CreateActorRequest(_message.Message):
    __slots__ = ("actor",)
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    actor: Actor
    def __init__(self, actor: _Optional[_Union[Actor, _Mapping]] = ...) -> None: ...

class UpdateActorRequest(_message.Message):
    __slots__ = ("actor",)
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    actor: Actor
    def __init__(self, actor: _Optional[_Union[Actor, _Mapping]] = ...) -> None: ...

class SuspendActorRequest(_message.Message):
    __slots__ = ("actor", "options")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    options: SuspendOptions
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., options: _Optional[_Union[SuspendOptions, _Mapping]] = ...) -> None: ...

class SuspendActorResponse(_message.Message):
    __slots__ = ("actor",)
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    actor: Actor
    def __init__(self, actor: _Optional[_Union[Actor, _Mapping]] = ...) -> None: ...

class SuspendOptions(_message.Message):
    __slots__ = ("mode",)
    MODE_FIELD_NUMBER: _ClassVar[int]
    mode: DurabilityMode
    def __init__(self, mode: _Optional[_Union[DurabilityMode, str]] = ...) -> None: ...

class ResumeActorRequest(_message.Message):
    __slots__ = ("actor",)
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class ResumeActorResponse(_message.Message):
    __slots__ = ("actor", "resumed")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    RESUMED_FIELD_NUMBER: _ClassVar[int]
    actor: Actor
    resumed: bool
    def __init__(self, actor: _Optional[_Union[Actor, _Mapping]] = ..., resumed: _Optional[bool] = ...) -> None: ...

class RevertActorRequest(_message.Message):
    __slots__ = ("actor",)
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class RevertActorResponse(_message.Message):
    __slots__ = ("actor",)
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    actor: Actor
    def __init__(self, actor: _Optional[_Union[Actor, _Mapping]] = ...) -> None: ...

class DeleteActorRequest(_message.Message):
    __slots__ = ("actor", "options")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    options: DeleteOptions
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., options: _Optional[_Union[DeleteOptions, _Mapping]] = ...) -> None: ...

class GetActorEgressPolicyRequest(_message.Message):
    __slots__ = ("actor", "name")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    name: str
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., name: _Optional[str] = ...) -> None: ...

class CreateActorEgressPolicyRequest(_message.Message):
    __slots__ = ("actor", "egress_policy")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    EGRESS_POLICY_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    egress_policy: EgressPolicy
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., egress_policy: _Optional[_Union[EgressPolicy, _Mapping]] = ...) -> None: ...

class UpdateActorEgressPolicyRequest(_message.Message):
    __slots__ = ("actor", "egress_policy")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    EGRESS_POLICY_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    egress_policy: EgressPolicy
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., egress_policy: _Optional[_Union[EgressPolicy, _Mapping]] = ...) -> None: ...

class DeleteActorEgressPolicyRequest(_message.Message):
    __slots__ = ("actor", "name", "options")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    name: str
    options: DeleteOptions
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., name: _Optional[str] = ..., options: _Optional[_Union[DeleteOptions, _Mapping]] = ...) -> None: ...

class GetTagRequest(_message.Message):
    __slots__ = ("tag",)
    TAG_FIELD_NUMBER: _ClassVar[int]
    tag: ObjectRef
    def __init__(self, tag: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class MintActorJwtRequest(_message.Message):
    __slots__ = ("actor", "audiences", "expiration_seconds")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    AUDIENCES_FIELD_NUMBER: _ClassVar[int]
    EXPIRATION_SECONDS_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    audiences: _containers.RepeatedScalarFieldContainer[str]
    expiration_seconds: int
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., audiences: _Optional[_Iterable[str]] = ..., expiration_seconds: _Optional[int] = ...) -> None: ...

class MintActorJwtResponse(_message.Message):
    __slots__ = ("actor_jwt", "expire_time")
    ACTOR_JWT_FIELD_NUMBER: _ClassVar[int]
    EXPIRE_TIME_FIELD_NUMBER: _ClassVar[int]
    actor_jwt: str
    expire_time: _timestamp_pb2.Timestamp
    def __init__(self, actor_jwt: _Optional[str] = ..., expire_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ListTagsRequest(_message.Message):
    __slots__ = ("atespace", "options")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    atespace: str
    options: ListOptions
    def __init__(self, atespace: _Optional[str] = ..., options: _Optional[_Union[ListOptions, _Mapping]] = ...) -> None: ...

class ListTagsResponse(_message.Message):
    __slots__ = ("tags", "next_page_token")
    TAGS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    tags: _containers.RepeatedCompositeFieldContainer[Tag]
    next_page_token: str
    def __init__(self, tags: _Optional[_Iterable[_Union[Tag, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class CreateTagRequest(_message.Message):
    __slots__ = ("tag",)
    TAG_FIELD_NUMBER: _ClassVar[int]
    tag: Tag
    def __init__(self, tag: _Optional[_Union[Tag, _Mapping]] = ...) -> None: ...

class UpdateTagRequest(_message.Message):
    __slots__ = ("tag",)
    TAG_FIELD_NUMBER: _ClassVar[int]
    tag: Tag
    def __init__(self, tag: _Optional[_Union[Tag, _Mapping]] = ...) -> None: ...

class DeleteTagRequest(_message.Message):
    __slots__ = ("tag", "options")
    TAG_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    tag: ObjectRef
    options: DeleteOptions
    def __init__(self, tag: _Optional[_Union[ObjectRef, _Mapping]] = ..., options: _Optional[_Union[DeleteOptions, _Mapping]] = ...) -> None: ...

class DeleteOptions(_message.Message):
    __slots__ = ("version", "uid")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    UID_FIELD_NUMBER: _ClassVar[int]
    version: int
    uid: str
    def __init__(self, version: _Optional[int] = ..., uid: _Optional[str] = ...) -> None: ...

class ListOptions(_message.Message):
    __slots__ = ("page_size", "page_token")
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    page_size: int
    page_token: str
    def __init__(self, page_size: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class ListWorkerActorAssignmentsRequest(_message.Message):
    __slots__ = ("worker", "options")
    WORKER_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    options: ListOptions
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ..., options: _Optional[_Union[ListOptions, _Mapping]] = ...) -> None: ...

class ListWorkerActorAssignmentsResponse(_message.Message):
    __slots__ = ("actor_assignments", "next_page_token")
    ACTOR_ASSIGNMENTS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    actor_assignments: _containers.RepeatedCompositeFieldContainer[ActorAssignment]
    next_page_token: str
    def __init__(self, actor_assignments: _Optional[_Iterable[_Union[ActorAssignment, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class ListWorkersRequest(_message.Message):
    __slots__ = ("options",)
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    options: ListOptions
    def __init__(self, options: _Optional[_Union[ListOptions, _Mapping]] = ...) -> None: ...

class ListWorkersResponse(_message.Message):
    __slots__ = ("workers", "next_page_token")
    WORKERS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    workers: _containers.RepeatedCompositeFieldContainer[Worker]
    next_page_token: str
    def __init__(self, workers: _Optional[_Iterable[_Union[Worker, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class GetWorkerRequest(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class CreateWorkerRequest(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: Worker
    def __init__(self, worker: _Optional[_Union[Worker, _Mapping]] = ...) -> None: ...

class UpdateWorkerRequest(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: Worker
    def __init__(self, worker: _Optional[_Union[Worker, _Mapping]] = ...) -> None: ...

class DeleteWorkerRequest(_message.Message):
    __slots__ = ("worker", "options")
    WORKER_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    options: DeleteOptions
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ..., options: _Optional[_Union[DeleteOptions, _Mapping]] = ...) -> None: ...

class DrainWorkerRequest(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class DrainWorkerResponse(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: Worker
    def __init__(self, worker: _Optional[_Union[Worker, _Mapping]] = ...) -> None: ...

class MarkWorkerAvailableRequest(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class MarkWorkerAvailableResponse(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: Worker
    def __init__(self, worker: _Optional[_Union[Worker, _Mapping]] = ...) -> None: ...

class MarkWorkerUnavailableRequest(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class MarkWorkerUnavailableResponse(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: Worker
    def __init__(self, worker: _Optional[_Union[Worker, _Mapping]] = ...) -> None: ...

class ListActorsRequest(_message.Message):
    __slots__ = ("atespace", "options")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    atespace: str
    options: ListOptions
    def __init__(self, atespace: _Optional[str] = ..., options: _Optional[_Union[ListOptions, _Mapping]] = ...) -> None: ...

class ListActorsResponse(_message.Message):
    __slots__ = ("actors", "next_page_token")
    ACTORS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    actors: _containers.RepeatedCompositeFieldContainer[Actor]
    next_page_token: str
    def __init__(self, actors: _Optional[_Iterable[_Union[Actor, _Mapping]]] = ..., next_page_token: _Optional[str] = ...) -> None: ...

class Worker(_message.Message):
    __slots__ = ("metadata", "worker_namespace", "worker_pool", "worker_pod", "worker_pod_uid", "node_name", "worker_spiffe_id", "labels", "status")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    METADATA_FIELD_NUMBER: _ClassVar[int]
    WORKER_NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    WORKER_POOL_FIELD_NUMBER: _ClassVar[int]
    WORKER_POD_FIELD_NUMBER: _ClassVar[int]
    WORKER_POD_UID_FIELD_NUMBER: _ClassVar[int]
    NODE_NAME_FIELD_NUMBER: _ClassVar[int]
    WORKER_SPIFFE_ID_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    metadata: ResourceMetadata
    worker_namespace: str
    worker_pool: str
    worker_pod: str
    worker_pod_uid: str
    node_name: str
    worker_spiffe_id: str
    labels: _containers.ScalarMap[str, str]
    status: WorkerStatus
    def __init__(self, metadata: _Optional[_Union[ResourceMetadata, _Mapping]] = ..., worker_namespace: _Optional[str] = ..., worker_pool: _Optional[str] = ..., worker_pod: _Optional[str] = ..., worker_pod_uid: _Optional[str] = ..., node_name: _Optional[str] = ..., worker_spiffe_id: _Optional[str] = ..., labels: _Optional[_Mapping[str, str]] = ..., status: _Optional[_Union[WorkerStatus, _Mapping]] = ...) -> None: ...

class WorkerStatus(_message.Message):
    __slots__ = ("state", "capacity", "allocated", "observed_epoch", "sandbox_classes", "ips", "issued_epoch", "registered_epoch")
    STATE_FIELD_NUMBER: _ClassVar[int]
    CAPACITY_FIELD_NUMBER: _ClassVar[int]
    ALLOCATED_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_EPOCH_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_CLASSES_FIELD_NUMBER: _ClassVar[int]
    IPS_FIELD_NUMBER: _ClassVar[int]
    ISSUED_EPOCH_FIELD_NUMBER: _ClassVar[int]
    REGISTERED_EPOCH_FIELD_NUMBER: _ClassVar[int]
    state: WorkerState
    capacity: WorkerResources
    allocated: WorkerResources
    observed_epoch: int
    sandbox_classes: _containers.RepeatedCompositeFieldContainer[WorkerSandboxClass]
    ips: _containers.RepeatedScalarFieldContainer[str]
    issued_epoch: int
    registered_epoch: int
    def __init__(self, state: _Optional[_Union[WorkerState, str]] = ..., capacity: _Optional[_Union[WorkerResources, _Mapping]] = ..., allocated: _Optional[_Union[WorkerResources, _Mapping]] = ..., observed_epoch: _Optional[int] = ..., sandbox_classes: _Optional[_Iterable[_Union[WorkerSandboxClass, _Mapping]]] = ..., ips: _Optional[_Iterable[str]] = ..., issued_epoch: _Optional[int] = ..., registered_epoch: _Optional[int] = ...) -> None: ...

class WorkerSandboxClass(_message.Message):
    __slots__ = ("sandbox_class", "default_runtime", "restorable_runtimes")
    SANDBOX_CLASS_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_RUNTIME_FIELD_NUMBER: _ClassVar[int]
    RESTORABLE_RUNTIMES_FIELD_NUMBER: _ClassVar[int]
    sandbox_class: str
    default_runtime: WorkerSandboxRuntime
    restorable_runtimes: _containers.RepeatedCompositeFieldContainer[WorkerSandboxRuntime]
    def __init__(self, sandbox_class: _Optional[str] = ..., default_runtime: _Optional[_Union[WorkerSandboxRuntime, _Mapping]] = ..., restorable_runtimes: _Optional[_Iterable[_Union[WorkerSandboxRuntime, _Mapping]]] = ...) -> None: ...

class WorkerSandboxRuntime(_message.Message):
    __slots__ = ("version", "actor_overhead")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    ACTOR_OVERHEAD_FIELD_NUMBER: _ClassVar[int]
    version: VersionedSandboxCompat
    actor_overhead: RuntimeOverhead
    def __init__(self, version: _Optional[_Union[VersionedSandboxCompat, _Mapping]] = ..., actor_overhead: _Optional[_Union[RuntimeOverhead, _Mapping]] = ...) -> None: ...

class RuntimeOverhead(_message.Message):
    __slots__ = ("memory_bytes", "memory_bytes_per_gib")
    MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    MEMORY_BYTES_PER_GIB_FIELD_NUMBER: _ClassVar[int]
    memory_bytes: int
    memory_bytes_per_gib: int
    def __init__(self, memory_bytes: _Optional[int] = ..., memory_bytes_per_gib: _Optional[int] = ...) -> None: ...

class ActorSandboxRuntime(_message.Message):
    __slots__ = ("sandbox_class", "version")
    SANDBOX_CLASS_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    sandbox_class: str
    version: VersionedSandboxCompat
    def __init__(self, sandbox_class: _Optional[str] = ..., version: _Optional[_Union[VersionedSandboxCompat, _Mapping]] = ...) -> None: ...

class VersionedSandboxCompat(_message.Message):
    __slots__ = ("schema_version", "attributes")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    schema_version: str
    attributes: _containers.RepeatedCompositeFieldContainer[AttributeEntry]
    def __init__(self, schema_version: _Optional[str] = ..., attributes: _Optional[_Iterable[_Union[AttributeEntry, _Mapping]]] = ...) -> None: ...

class AttributeEntry(_message.Message):
    __slots__ = ("key", "value")
    KEY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    key: str
    value: str
    def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class WorkerResources(_message.Message):
    __slots__ = ("cpu_millicores", "memory_bytes", "actors")
    CPU_MILLICORES_FIELD_NUMBER: _ClassVar[int]
    MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    ACTORS_FIELD_NUMBER: _ClassVar[int]
    cpu_millicores: int
    memory_bytes: int
    actors: int
    def __init__(self, cpu_millicores: _Optional[int] = ..., memory_bytes: _Optional[int] = ..., actors: _Optional[int] = ...) -> None: ...

class ActorAssignment(_message.Message):
    __slots__ = ("metadata", "actor", "actor_uid", "resources")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    ACTOR_UID_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    metadata: ResourceMetadata
    actor: ObjectRef
    actor_uid: str
    resources: Resources
    def __init__(self, metadata: _Optional[_Union[ResourceMetadata, _Mapping]] = ..., actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., actor_uid: _Optional[str] = ..., resources: _Optional[_Union[Resources, _Mapping]] = ...) -> None: ...

class AcquireWorkerEpochRequest(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class AcquireWorkerEpochResponse(_message.Message):
    __slots__ = ("epoch",)
    EPOCH_FIELD_NUMBER: _ClassVar[int]
    epoch: int
    def __init__(self, epoch: _Optional[int] = ...) -> None: ...

class RegisterWorkerRequest(_message.Message):
    __slots__ = ("worker", "epoch", "ips", "capacity")
    WORKER_FIELD_NUMBER: _ClassVar[int]
    EPOCH_FIELD_NUMBER: _ClassVar[int]
    IPS_FIELD_NUMBER: _ClassVar[int]
    CAPACITY_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    epoch: int
    ips: _containers.RepeatedScalarFieldContainer[str]
    capacity: WorkerResources
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ..., epoch: _Optional[int] = ..., ips: _Optional[_Iterable[str]] = ..., capacity: _Optional[_Union[WorkerResources, _Mapping]] = ...) -> None: ...

class RegisterWorkerResponse(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: Worker
    def __init__(self, worker: _Optional[_Union[Worker, _Mapping]] = ...) -> None: ...

class SetWorkerSandboxRuntimesRequest(_message.Message):
    __slots__ = ("worker", "epoch", "sandbox_classes")
    WORKER_FIELD_NUMBER: _ClassVar[int]
    EPOCH_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_CLASSES_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    epoch: int
    sandbox_classes: _containers.RepeatedCompositeFieldContainer[WorkerSandboxClass]
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ..., epoch: _Optional[int] = ..., sandbox_classes: _Optional[_Iterable[_Union[WorkerSandboxClass, _Mapping]]] = ...) -> None: ...

class SetWorkerSandboxRuntimesResponse(_message.Message):
    __slots__ = ("worker",)
    WORKER_FIELD_NUMBER: _ClassVar[int]
    worker: Worker
    def __init__(self, worker: _Optional[_Union[Worker, _Mapping]] = ...) -> None: ...

class MintWorkerActorCertificateRequest(_message.Message):
    __slots__ = ("actor", "actor_uid", "certificate_signing_request", "worker")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    ACTOR_UID_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_SIGNING_REQUEST_FIELD_NUMBER: _ClassVar[int]
    WORKER_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    actor_uid: str
    certificate_signing_request: bytes
    worker: ObjectRef
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., actor_uid: _Optional[str] = ..., certificate_signing_request: _Optional[bytes] = ..., worker: _Optional[_Union[ObjectRef, _Mapping]] = ...) -> None: ...

class MintWorkerActorCertificateResponse(_message.Message):
    __slots__ = ("actor_certificates",)
    ACTOR_CERTIFICATES_FIELD_NUMBER: _ClassVar[int]
    actor_certificates: _containers.RepeatedScalarFieldContainer[bytes]
    def __init__(self, actor_certificates: _Optional[_Iterable[bytes]] = ...) -> None: ...

class MintWorkerActorJwtRequest(_message.Message):
    __slots__ = ("actor", "actor_uid", "audiences")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    ACTOR_UID_FIELD_NUMBER: _ClassVar[int]
    AUDIENCES_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    actor_uid: str
    audiences: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., actor_uid: _Optional[str] = ..., audiences: _Optional[_Iterable[str]] = ...) -> None: ...

class MintWorkerActorJwtResponse(_message.Message):
    __slots__ = ("actor_jwt", "expire_time")
    ACTOR_JWT_FIELD_NUMBER: _ClassVar[int]
    EXPIRE_TIME_FIELD_NUMBER: _ClassVar[int]
    actor_jwt: str
    expire_time: _timestamp_pb2.Timestamp
    def __init__(self, actor_jwt: _Optional[str] = ..., expire_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class RequestActorSuspendRequest(_message.Message):
    __slots__ = ("worker", "actor", "actor_uid")
    WORKER_FIELD_NUMBER: _ClassVar[int]
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    ACTOR_UID_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    actor: ObjectRef
    actor_uid: str
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ..., actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., actor_uid: _Optional[str] = ...) -> None: ...

class RequestActorSuspendResponse(_message.Message):
    __slots__ = ("actor",)
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    actor: Actor
    def __init__(self, actor: _Optional[_Union[Actor, _Mapping]] = ...) -> None: ...

class ReportActorCrashedRequest(_message.Message):
    __slots__ = ("worker", "actor", "actor_uid", "message")
    WORKER_FIELD_NUMBER: _ClassVar[int]
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    ACTOR_UID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    worker: ObjectRef
    actor: ObjectRef
    actor_uid: str
    message: str
    def __init__(self, worker: _Optional[_Union[ObjectRef, _Mapping]] = ..., actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., actor_uid: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class ReportActorCrashedResponse(_message.Message):
    __slots__ = ("actor",)
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    actor: Actor
    def __init__(self, actor: _Optional[_Union[Actor, _Mapping]] = ...) -> None: ...

class ReportActorSnapshotStateRequest(_message.Message):
    __slots__ = ("actor", "actor_uid", "state")
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    ACTOR_UID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    actor: ObjectRef
    actor_uid: str
    state: ActorSnapshotState
    def __init__(self, actor: _Optional[_Union[ObjectRef, _Mapping]] = ..., actor_uid: _Optional[str] = ..., state: _Optional[_Union[ActorSnapshotState, _Mapping]] = ...) -> None: ...

class ReportActorSnapshotStateResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ActorSnapshotState(_message.Message):
    __slots__ = ("latest_local", "uploading", "latest_durable", "upload_due_time")
    LATEST_LOCAL_FIELD_NUMBER: _ClassVar[int]
    UPLOADING_FIELD_NUMBER: _ClassVar[int]
    LATEST_DURABLE_FIELD_NUMBER: _ClassVar[int]
    UPLOAD_DUE_TIME_FIELD_NUMBER: _ClassVar[int]
    latest_local: SnapshotReport
    uploading: SnapshotReport
    latest_durable: SnapshotReport
    upload_due_time: _timestamp_pb2.Timestamp
    def __init__(self, latest_local: _Optional[_Union[SnapshotReport, _Mapping]] = ..., uploading: _Optional[_Union[SnapshotReport, _Mapping]] = ..., latest_durable: _Optional[_Union[SnapshotReport, _Mapping]] = ..., upload_due_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SnapshotReport(_message.Message):
    __slots__ = ("uuid", "generation", "fidelity", "object", "local_commit_time", "durable_complete_time")
    UUID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    FIDELITY_FIELD_NUMBER: _ClassVar[int]
    OBJECT_FIELD_NUMBER: _ClassVar[int]
    LOCAL_COMMIT_TIME_FIELD_NUMBER: _ClassVar[int]
    DURABLE_COMPLETE_TIME_FIELD_NUMBER: _ClassVar[int]
    uuid: str
    generation: int
    fidelity: SnapshotFidelity
    object: ObjectSnapshot
    local_commit_time: _timestamp_pb2.Timestamp
    durable_complete_time: _timestamp_pb2.Timestamp
    def __init__(self, uuid: _Optional[str] = ..., generation: _Optional[int] = ..., fidelity: _Optional[_Union[SnapshotFidelity, str]] = ..., object: _Optional[_Union[ObjectSnapshot, _Mapping]] = ..., local_commit_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., durable_complete_time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class AccessPolicy(_message.Message):
    __slots__ = ("metadata", "bindings")
    METADATA_FIELD_NUMBER: _ClassVar[int]
    BINDINGS_FIELD_NUMBER: _ClassVar[int]
    metadata: ResourceMetadata
    bindings: _containers.RepeatedCompositeFieldContainer[Binding]
    def __init__(self, metadata: _Optional[_Union[ResourceMetadata, _Mapping]] = ..., bindings: _Optional[_Iterable[_Union[Binding, _Mapping]]] = ...) -> None: ...

class Binding(_message.Message):
    __slots__ = ("role", "members")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    MEMBERS_FIELD_NUMBER: _ClassVar[int]
    role: str
    members: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, role: _Optional[str] = ..., members: _Optional[_Iterable[str]] = ...) -> None: ...

class GetGlobalAccessPolicyRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class CreateGlobalAccessPolicyRequest(_message.Message):
    __slots__ = ("access_policy",)
    ACCESS_POLICY_FIELD_NUMBER: _ClassVar[int]
    access_policy: AccessPolicy
    def __init__(self, access_policy: _Optional[_Union[AccessPolicy, _Mapping]] = ...) -> None: ...

class UpdateGlobalAccessPolicyRequest(_message.Message):
    __slots__ = ("access_policy",)
    ACCESS_POLICY_FIELD_NUMBER: _ClassVar[int]
    access_policy: AccessPolicy
    def __init__(self, access_policy: _Optional[_Union[AccessPolicy, _Mapping]] = ...) -> None: ...

class GetAtespaceAccessPolicyRequest(_message.Message):
    __slots__ = ("atespace", "name")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    atespace: ObjectRef
    name: str
    def __init__(self, atespace: _Optional[_Union[ObjectRef, _Mapping]] = ..., name: _Optional[str] = ...) -> None: ...

class CreateAtespaceAccessPolicyRequest(_message.Message):
    __slots__ = ("atespace", "access_policy")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    ACCESS_POLICY_FIELD_NUMBER: _ClassVar[int]
    atespace: ObjectRef
    access_policy: AccessPolicy
    def __init__(self, atespace: _Optional[_Union[ObjectRef, _Mapping]] = ..., access_policy: _Optional[_Union[AccessPolicy, _Mapping]] = ...) -> None: ...

class UpdateAtespaceAccessPolicyRequest(_message.Message):
    __slots__ = ("atespace", "access_policy")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    ACCESS_POLICY_FIELD_NUMBER: _ClassVar[int]
    atespace: ObjectRef
    access_policy: AccessPolicy
    def __init__(self, atespace: _Optional[_Union[ObjectRef, _Mapping]] = ..., access_policy: _Optional[_Union[AccessPolicy, _Mapping]] = ...) -> None: ...

class DeleteAtespaceAccessPolicyRequest(_message.Message):
    __slots__ = ("atespace", "name", "options")
    ATESPACE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    atespace: ObjectRef
    name: str
    options: DeleteOptions
    def __init__(self, atespace: _Optional[_Union[ObjectRef, _Mapping]] = ..., name: _Optional[str] = ..., options: _Optional[_Union[DeleteOptions, _Mapping]] = ...) -> None: ...

class ResourceAnnotation(_message.Message):
    __slots__ = ("parents", "singleton", "scope", "plural")
    PARENTS_FIELD_NUMBER: _ClassVar[int]
    SINGLETON_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    PLURAL_FIELD_NUMBER: _ClassVar[int]
    parents: _containers.RepeatedScalarFieldContainer[str]
    singleton: bool
    scope: Scope
    plural: str
    def __init__(self, parents: _Optional[_Iterable[str]] = ..., singleton: _Optional[bool] = ..., scope: _Optional[_Union[Scope, str]] = ..., plural: _Optional[str] = ...) -> None: ...

class MethodAnnotation(_message.Message):
    __slots__ = ("resource",)
    RESOURCE_FIELD_NUMBER: _ClassVar[int]
    resource: str
    def __init__(self, resource: _Optional[str] = ...) -> None: ...
