# Named TCP egress configuration examples

`local-http.yaml` and `public-ssh.yaml` illustrate a single named TCP route
through `omnigent-egress-connect`, with direct networking disabled. They are
configuration references, not runnable end-to-end Claude SDK demos.

Both files select the Claude SDK harness and set `allow_network: false`. That
restriction applies to the whole Claude CLI process, including its model
provider connections. Configuring model credentials with `omnigent setup` or
passing `--model` does not grant network access. Running these files with a
network-based provider therefore fails to reach the model; it cannot perform
the prompted connector checks. An unwrapped CLI fallback is not supported.

Named TCP routes grant access only through the connector. They do not authorize
ordinary provider HTTPS requests. Supporting these examples as agent-driven
demos requires a separately supported model transport compatible with the
requested confinement. Do not enable unrestricted networking to make the
network-denial checks appear to pass.

## Local connector verification

The isolated connector test exercises a local echo fixture, a granted route,
an ungranted route, and broker revocation without requiring a model provider:

```sh
PYTHONPATH="$PWD:$PWD/sdks/python-client:$PWD/sdks/ui" \
  uv run --no-sync pytest tests/inner/sandbox/test_tcp_egress.py \
  -k test_tcp_connector_round_trip_and_revocation --reruns 0 -q
```

This verifies the connector contract; it does not establish that a Claude
agent can reach its model while whole-process networking is disabled.

## Route templates

`local-http.yaml` uses the documentation address `192.0.2.10`, port `8765`,
and the exact CIDR `192.0.2.10/32`. For a listener you control, replace all
three address occurrences with its reachable IPv4 address. Loopback and
link-local destinations remain denied. The prompt documents the expected
connector response, direct-connection denial, and ungranted-route denial for
a compatible execution environment.

`public-ssh.yaml` grants `github.com:22` and describes reading an SSH greeting
without authentication. The broker requires every DNS answer to be public;
networks that block port 22 prevent the connection. A greeting does not verify
the remote server's identity. This template has the same model-connectivity
limitation as the local example.

`sandbox.type: auto` selects Seatbelt on macOS and Bubblewrap on Linux; Linux
requires `bwrap` and enabled user namespaces. Opaque TCP routes cannot use ports
80 or 443 and cannot coexist with HTTP `egress_rules`.
