from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RouteOption(_message.Message):
    __slots__ = ("model", "harness")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    HARNESS_FIELD_NUMBER: _ClassVar[int]
    model: str
    harness: str
    def __init__(self, model: _Optional[str] = ..., harness: _Optional[str] = ...) -> None: ...

class RouteSelector(_message.Message):
    __slots__ = ("router_name", "config")
    ROUTER_NAME_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    router_name: str
    config: _struct_pb2.Struct
    def __init__(self, router_name: _Optional[str] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class RouteSelection(_message.Message):
    __slots__ = ("route_option", "params")
    ROUTE_OPTION_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    route_option: RouteOption
    params: _struct_pb2.Struct
    def __init__(self, route_option: _Optional[_Union[RouteOption, _Mapping]] = ..., params: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class Task(_message.Message):
    __slots__ = ("prompt",)
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    prompt: str
    def __init__(self, prompt: _Optional[str] = ...) -> None: ...

class SessionTurn(_message.Message):
    __slots__ = ("task", "route_selection")
    TASK_FIELD_NUMBER: _ClassVar[int]
    ROUTE_SELECTION_FIELD_NUMBER: _ClassVar[int]
    task: Task
    route_selection: RouteSelection
    def __init__(self, task: _Optional[_Union[Task, _Mapping]] = ..., route_selection: _Optional[_Union[RouteSelection, _Mapping]] = ...) -> None: ...

class SessionHistory(_message.Message):
    __slots__ = ("session_turns",)
    SESSION_TURNS_FIELD_NUMBER: _ClassVar[int]
    session_turns: _containers.RepeatedCompositeFieldContainer[SessionTurn]
    def __init__(self, session_turns: _Optional[_Iterable[_Union[SessionTurn, _Mapping]]] = ...) -> None: ...

class SelectRouteRequest(_message.Message):
    __slots__ = ("route_options", "task", "route_selector", "session_history")
    ROUTE_OPTIONS_FIELD_NUMBER: _ClassVar[int]
    TASK_FIELD_NUMBER: _ClassVar[int]
    ROUTE_SELECTOR_FIELD_NUMBER: _ClassVar[int]
    SESSION_HISTORY_FIELD_NUMBER: _ClassVar[int]
    route_options: _containers.RepeatedCompositeFieldContainer[RouteOption]
    task: Task
    route_selector: RouteSelector
    session_history: SessionHistory
    def __init__(self, route_options: _Optional[_Iterable[_Union[RouteOption, _Mapping]]] = ..., task: _Optional[_Union[Task, _Mapping]] = ..., route_selector: _Optional[_Union[RouteSelector, _Mapping]] = ..., session_history: _Optional[_Union[SessionHistory, _Mapping]] = ...) -> None: ...

class SelectRouteResponse(_message.Message):
    __slots__ = ("route_selection", "rationale")
    ROUTE_SELECTION_FIELD_NUMBER: _ClassVar[int]
    RATIONALE_FIELD_NUMBER: _ClassVar[int]
    route_selection: _containers.RepeatedCompositeFieldContainer[RouteSelection]
    rationale: str
    def __init__(self, route_selection: _Optional[_Iterable[_Union[RouteSelection, _Mapping]]] = ..., rationale: _Optional[str] = ...) -> None: ...
