"""The workspace API's methods, read from its proto, for code that forwards them.

The host's tunnel and the server's route handle every method the same way, so
they share one table instead of one branch per method. A name outside the
table is never dispatched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from google.protobuf import message_factory
from google.protobuf.message import Message

from omnigent.api.workspace.v1alpha1 import workspace_pb2


@dataclass(frozen=True)
class WorkspaceMethod:
    """One WorkspaceService method.

    :param name: As GetCapabilities lists it, e.g. ``"SearchFiles"``.
    :param python_name: The service and client attribute, e.g. ``"search_files"``.
    :param request_type: Its request message class.
    :param response_type: Its response message class.
    :param streaming: Whether it streams responses.
    """

    name: str
    python_name: str
    request_type: type[Message]
    response_type: type[Message]
    streaming: bool


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


WORKSPACE_METHODS: dict[str, WorkspaceMethod] = {
    method.name: WorkspaceMethod(
        name=method.name,
        python_name=_snake_case(method.name),
        request_type=message_factory.GetMessageClass(method.input_type),
        response_type=message_factory.GetMessageClass(method.output_type),
        streaming=method.server_streaming,
    )
    for method in workspace_pb2.DESCRIPTOR.services_by_name["WorkspaceService"].methods
}
