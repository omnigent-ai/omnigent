"""Run the real server with item-payload encryption served by an in-process gRPC CMK stub.

Stands in for the Databricks agentbricks/mas deployment, where the MySQL/CMK
conversation store encrypts every conversation-item payload through the
mas-java sidecar's ``EncryptPayloads`` / ``DecryptPayloads`` RPCs behind the
Barnacle forward-proxy. Here the RPC is served in-process and returns the
payloads unchanged. While the file named by ``OMNIGENT_E2E_CMK_DENY_FLAG``
exists, every call is rejected with ``PERMISSION_DENIED`` and the detail string
``"Received http2 header with status: 403"`` — the exact rejection the ticket
quotes — so the store raises the same ``_InactiveRpcError`` the deployment's
``_cmk_rpc`` does.

Usage::

    OMNIGENT_E2E_CMK_DENY_FLAG=/tmp/cmk-deny \\
      python -m tests.e2e_ui.chat._cmk_denying_store_server \\
      --host 127.0.0.1 --port 18767 --database-uri sqlite:////tmp/cmk.db \\
      --artifact-location /tmp/cmk-artifacts --agent hello_world.yaml

All arguments go to the normal ``omnigent server`` CLI; only the store's
batch encode/decode hooks are replaced.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent import futures
from pathlib import Path
from typing import Any
from unittest.mock import patch

import grpc

DENY_FLAG_ENV = "OMNIGENT_E2E_CMK_DENY_FLAG"
CMK_SERVICE = "mas.MasJavaService"
ENCRYPT_METHOD = "EncryptPayloads"
DECRYPT_METHOD = "DecryptPayloads"
DENIED_DETAILS = "Received http2 header with status: 403"
# The deployment's CMK calls run with a 30 s deadline.
_RPC_TIMEOUT_S = 30.0


def _start_cmk_service(deny_flag: Path) -> tuple[grpc.Server, str]:
    """
    Start the in-process CMK gRPC service.

    :param deny_flag: File whose presence turns every call into a
        ``PERMISSION_DENIED`` rejection.
    :returns: The started service (a dropped ``grpc.Server`` stops itself)
        and its ``host:port`` target.
    """

    def _handle(request: bytes, context: grpc.ServicerContext) -> bytes:
        if deny_flag.exists():
            context.abort(grpc.StatusCode.PERMISSION_DENIED, DENIED_DETAILS)
        return request

    handler = grpc.unary_unary_rpc_method_handler(_handle)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                CMK_SERVICE,
                {ENCRYPT_METHOD: handler, DECRYPT_METHOD: handler},
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, f"127.0.0.1:{port}"


def main() -> None:
    """Route the store's payload encode/decode through the CMK stub, then run the CLI."""
    import omnigent.stores.conversation_store.sqlalchemy_store as store_module
    from omnigent.cli import cli

    cmk_service, target = _start_cmk_service(Path(os.environ[DENY_FLAG_ENV]))
    channel = grpc.insecure_channel(target)

    def _stub(method: str) -> grpc.UnaryUnaryMultiCallable:
        return channel.unary_unary(
            f"/{CMK_SERVICE}/{method}",
            request_serializer=lambda payload: payload,
            response_deserializer=lambda payload: payload,
        )

    encrypt = _stub(ENCRYPT_METHOD)
    decrypt = _stub(DECRYPT_METHOD)

    def _cmk_rpc(stub: grpc.UnaryUnaryMultiCallable, payloads: list[str]) -> list[str]:
        response, _call = stub.with_call(json.dumps(payloads).encode(), timeout=_RPC_TIMEOUT_S)
        return json.loads(response)

    def _encode_item_data_batch(self: Any, data_jsons: list[str]) -> list[str]:
        return _cmk_rpc(encrypt, data_jsons)

    def _decode_item_data_batch(self: Any, stored: list[str]) -> list[str]:
        return _cmk_rpc(decrypt, stored)

    store_cls = store_module.SqlAlchemyConversationStore
    with (
        patch.object(store_cls, "_encode_item_data_batch", _encode_item_data_batch),
        patch.object(store_cls, "_decode_item_data_batch", _decode_item_data_batch),
    ):
        cli(args=["server", *sys.argv[1:]], prog_name="cmk-store-test-server")
    cmk_service.stop(grace=None)


if __name__ == "__main__":
    main()
