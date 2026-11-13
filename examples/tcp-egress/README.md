# Named TCP egress from the CLI

These examples let an agent reach one host and port through
`omnigent-egress-connect`, while direct network connections remain blocked.
They use `sys_os_shell` with the Claude SDK harness. Configure its credentials
with `omnigent setup` first; optionally pass `--model` when running the agent.
Use a build containing named TCP egress on the server, host, and runner.

`sandbox.type: auto` selects Seatbelt on macOS and Bubblewrap on Linux. Linux
needs `bwrap` installed and user namespaces enabled. Neither example needs
credentials for the destination service.

The commands below use the repository's `.venv` and set `PYTHONPATH` so the CLI,
server, and runner import this checkout, including when worktrees share a virtual
environment. Run them from the root of the checkout containing this feature.

## Local Python HTTP listener

Run these commands from the repository root in terminal A. The UDP socket below
only discovers the local interface address; it sends no packets. If it selects
the wrong interface, set `TCP_TEST_IP` to your machine's LAN IPv4 address instead.
Loopback (`127.0.0.1`, `::1`) and link-local addresses are always denied, even
with an explicit CIDR grant.

```sh
export TCP_TEST_IP="$(python3 - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
    probe.connect(('192.0.2.1', 80))
    print(probe.getsockname()[0])
PY
)"
export TCP_DEMO_DIR="$(mktemp -d /tmp/omnigent-tcp-demo.XXXXXX)"
mkdir "$TCP_DEMO_DIR/www"
python3 - <<'PY'
import ipaddress
import os
import secrets
from pathlib import Path

address = ipaddress.IPv4Address(os.environ['TCP_TEST_IP'])
assert not (address.is_loopback or address.is_link_local or address.is_unspecified)
root = Path(os.environ['TCP_DEMO_DIR'])
template = Path('examples/tcp-egress/local-http.yaml').read_text()
(root / 'local-http.yaml').write_text(template.replace('192.0.2.10', str(address)))
(root / 'www' / 'marker.txt').write_text('tcp-egress-ok-' + secrets.token_hex(8) + '\n')
print(f'Listener: http://{address}:8765/marker.txt')
print(f'Agent YAML: {root}/local-http.yaml')
PY
python3 -m http.server 8765 --bind "$TCP_TEST_IP" --directory "$TCP_DEMO_DIR/www"
```

The listener serves only the temporary `www` directory, on the selected
interface. Keep it running. In terminal B, stay in the repository root, set the
path printed above, and run:

```sh
# Replace this with the Agent YAML path printed in terminal A.
export TCP_DEMO_YAML=/tmp/omnigent-tcp-demo.REPLACE/local-http.yaml
PYTHONPATH="$PWD:$PWD/sdks/python-client:$PWD/sdks/ui" \
OMNIGENT_DATABASE_URI="sqlite:///$HOME/.omnigent/chat.db" \
  .venv/bin/python -m omnigent.cli run "$TCP_DEMO_YAML" --server local \
  -p 'Run all three network checks in your instructions and show their results.'
```

For an installed build containing this feature, use `omnigent run` in place of
`.venv/bin/python -m omnigent.cli run`; `PYTHONPATH` is only needed for the source
checkout. `--server local` overrides a configured remote server. Restart an older
local server/host before testing this build.

Expected results:

- The connector returns **HTTP 200** and a `tcp-egress-ok-…` marker. Terminal A
  logs exactly that successful `GET /marker.txt` request. Compare the returned
  marker with the file in terminal A if desired.
- Direct `socket.create_connection` to the same IP and port fails with a sandbox
  network error. A host firewall failure alone does not prove sandbox enforcement;
  the connector request must succeed in the same run.
- `ungranted_route` fails with `TCP egress route is not allowed`.

The generated grant is exactly `host: YOUR_LAN_IP`, `port: 8765`, and
`allowed_cidrs: [YOUR_LAN_IP/32]`. Changing the listener port requires changing
both the grant and the direct-connection check. Do not add HTTP `egress_rules`:
those cannot coexist with `tcp_egress`. Ports 80 and 443 are also rejected for
opaque TCP routes.

Stop the listener with Ctrl-C. Once the agent has exited, remove its temporary
directory from terminal A with `rm -r "$TCP_DEMO_DIR"`.

## Public Internet service

From the repository root:

```sh
PYTHONPATH="$PWD:$PWD/sdks/python-client:$PWD/sdks/ui" \
OMNIGENT_DATABASE_URI="sqlite:///$HOME/.omnigent/chat.db" \
  .venv/bin/python -m omnigent.cli run examples/tcp-egress/public-ssh.yaml --server local \
  -p 'Read the public SSH banner using the granted connector and report it.'
```

This grants `github.com:22` and reads its SSH greeting without logging in. Expect
an `SSH-2.0-…` line. The command holds stdin open for five seconds before sending
EOF so the server has time to greet the client. The broker requires all DNS
answers to be public; no private
CIDR grant is needed. An external network that blocks port 22 will prevent this
test. Reading a banner does not verify the remote server's identity.

For a public HTTP listener you control, run the same Python listener on its
public interface at port 8765. Copy `local-http.yaml`, replace `192.0.2.10` with
that server's reachable public IPv4 address in all three places, and run the
copy with the same CLI command. Allow inbound TCP 8765 through that server's
firewall for the execution host, then remove the rule and stop the listener
after testing. The agent still receives only the one IP/port grant.
