# Substrate protocol bindings

`v1/ateapi.proto` is copied unchanged from Agent Substrate v1.0.0,
commit `b34fbee742906494b5eb4f1f36987176bf8770a4`:
https://github.com/agent-substrate/substrate/blob/v1.0.0/api/ateapi/v1/ateapi.proto

Source SHA-256: `b5bb348f04b8f8d9f24c4c880e1f4176771e1556cfd0cff95a97ca8bee079722`.
The schema retains its upstream Apache-2.0 copyright and license notice.
Generated bindings derive from that schema. Keep the complete upstream schema,
including its annotations; do not hand-edit wire messages or generated files.

Regenerate from the repository root with the repository's lint dependencies:

```sh
uv run --no-sync python scripts/gen_protobuf.py
```

The generator normalizes protoc's invalid module-level `ClassVar` annotations;
class attributes are unchanged. The `protobuf-bindings-fresh` hook verifies the result.

The launcher uses the generated Control service descriptors with gRPC's unary
client, so no additional generated server implementation is needed.
