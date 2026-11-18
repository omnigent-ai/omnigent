from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class GetInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class GetInfoResponse(_message.Message):
    __slots__ = ("protocol_version", "version", "commit", "capabilities")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    COMMIT_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    protocol_version: int
    version: str
    commit: str
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, protocol_version: _Optional[int] = ..., version: _Optional[str] = ..., commit: _Optional[str] = ..., capabilities: _Optional[_Iterable[str]] = ...) -> None: ...

class OpenWorkspaceRequest(_message.Message):
    __slots__ = ("root", "ahead_of_search")
    ROOT_FIELD_NUMBER: _ClassVar[int]
    AHEAD_OF_SEARCH_FIELD_NUMBER: _ClassVar[int]
    root: str
    ahead_of_search: bool
    def __init__(self, root: _Optional[str] = ..., ahead_of_search: _Optional[bool] = ...) -> None: ...

class OpenWorkspaceResponse(_message.Message):
    __slots__ = ("workspace_id", "root", "repository_root")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    ROOT_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_ROOT_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    root: str
    repository_root: str
    def __init__(self, workspace_id: _Optional[str] = ..., root: _Optional[str] = ..., repository_root: _Optional[str] = ...) -> None: ...

class CloseWorkspaceRequest(_message.Message):
    __slots__ = ("workspace_id",)
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    def __init__(self, workspace_id: _Optional[str] = ...) -> None: ...

class CloseWorkspaceResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
