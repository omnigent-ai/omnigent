# Test primitives

Start with the nearest existing scenario and the [test placement guide](../README.md).
Reuse sufficient coverage unchanged; otherwise extend a compatible scenario before
adding another test body. These helpers remove repeated mechanics while callers
retain configuration, fault injection and behavioral assertions.

| Need | Primitive | Existing example |
| --- | --- | --- |
| Bundle bytes and upload with caller metadata/client | `bundle_files`, `post_session_bundle` in [session.py](session.py) | [Headless subagent](../e2e/test_codex_native_headless_subagent_e2e.py) |
| Trusted fixture with Python tools, custom policies, or host paths | `operator_agent_store`, `post_operator_session` in [operator_agents.py](operator_agents.py) | [Image result policies](../integration/test_mcp_image_http_round_trip.py) |
| Browser-suite session for an explicit identity | `create_user_session` in [_multi_user_server.py](../e2e_ui/collaboration/_multi_user_server.py) | [Default-public sessions](../e2e_ui/collaboration/test_default_public_sessions.py) |
| Isolated local server | `isolated_local_server` in [live_server.py](live_server.py) | [Multi-user server](../e2e_ui/collaboration/_multi_user_server.py) |
| Trusted local HTTPS for a fake upstream | `enable_https` in [https_server.py](https_server.py) | [Native agy gateway](../e2e_ui/shells/test_antigravity_gateway_setup.py) |
| Port held until its process binds it | `reserve_port` in [live_server.py](live_server.py) | [Resilience lab](../e2e/resilience/lab/lab.py) |
| Poll a condition until it holds, sync or async | `poll_until`, `poll_until_async` in [polling.py](polling.py) | [Approval card](../e2e_ui/approvals/test_approval_card.py), [start session](../e2e_ui/start_session/test_start_session.py) |
| Server and runner lifecycle | `server_runner` in [server_runner.py](server_runner.py) | [Runner shutdown](../e2e/test_runner_shutdown_mcp_notify_hang_e2e.py) |
| Real host with isolated identity/home | `isolated_host` in [host.py](host.py) | [Host reconnect](../e2e/test_host_transient_404_restart.py) |
| Per-user page, sync or async | `user_page`, `async_user_page` in [browser.py](browser.py) | [Permissions](../e2e_ui/collaboration/test_permissions_modal.py), [async readiness](../e2e_ui/chat/test_credentialless_host_harness_readiness.py) |
| Native-wrapper session | `create_native_session` in [native_session.py](native_session.py) | [Native workspace](../e2e_ui/messages/test_native_codex_workspace_cwd.py) |
| Local Codex marketplace/plugin install | `install_codex_plugin` in [codex_plugins.py](codex_plugins.py), with caller-owned home/workspace and names | [Plugin catalog](../harnesses/codex_native/test_skill_discovery.py), [both composers](../e2e_ui/chat/test_codex_plugin_skills.py) |
| Provider resolution without the developer's config, overlay or ambient credentials | `isolate_provider_resolution` in [providers.py](providers.py) | [Host model options](../host/test_connect.py) |
| Hermetic stdio ACP agent process | `FAKE_ACP_AGENT_SCRIPT`, `FAKE_ACP_AGENT_REPLY` in [acp_agent.py](acp_agent.py) | [Files tab survives an ACP reply](../e2e_ui/files/test_files_tab_survives_acp_reply.py) |

## Session inputs stay explicit

For browser-suite tests with an existing server URL and caller-owned agent bundle:

```python
from tests.e2e_ui.collaboration._multi_user_server import create_user_session

session_id = create_user_session(
    base_url,
    headers={"X-Forwarded-Email": "owner@example.test"},
    metadata={"title": "Shared workspace"},
    bundle=bundle,
)
```

This convenience function requires success and returns the session ID. Its module
imports browser fixtures and Playwright; non-browser callers should use
`post_session_bundle` and explicitly check success. For a negative request or an
exact response assertion, also use `post_session_bundle` with
the caller's client instead. This relative URL requires an `httpx.Client` with
`base_url` set to the test server (otherwise pass an absolute URL):

```python
from tests._helpers.session import post_session_bundle

response = post_session_bundle(
    client.post, "/v1/sessions", bundle, metadata=metadata, headers=headers
)
assert response.status_code == 403
```

`bundle_files` archives caller-supplied member names/bytes. Use
`create_native_session(client, base_url, harness="codex", metadata=metadata)` for
the production native-wrapper spec; it returns a response dict with `session_id`.
Its supported harnesses and model restrictions are documented in the function.
Neither upload helper binds a runner or deletes the session. Bind explicitly with
`bind_session_runner` when needed: binding can start native terminals.

## Own processes, preserve their configuration

Given pytest's `tmp_path`, an existing `live_server` and an authenticated
`httpx.Client(base_url=live_server)` (also used by `wait_online`):

```python
from tests._helpers.host import isolated_host

with isolated_host(tmp_path / "host", live_server, env=host_env) as host:
    host.wait_online(client, timeout=45, poll_interval=0.1)
    response = client.get(f"/v1/hosts/{host.host_id}/plugins")
    assert response.status_code == 200
```

`isolated_host` owns home/config/data, unique identity, logs and process cleanup.
Use a fresh root. It accepts `host_id`, `name`, `command`, `cwd`, environment
overrides/removals and an optional readiness callback. Pass `config` to seed
provider or other non-identity settings before the daemon starts. Readiness is explicit:
call `wait_online` or provide the callback. Do not use its preseeded identity to
test identity generation or malformed configuration. Keep those setups local.
Set identity through `host_id` and `name`; `env` cannot override the helper-owned
config, identity or process-log keys.

For a standalone server, `with isolated_local_server(root, env=server_env) as
base_url:` owns startup/readiness/cleanup. It defaults to single-user header auth;
multi-user tests override that setting. Existing profile or compatibility tests
should retain `start_live_server` or their corresponding fixture.

For a server and runner, start the runner explicitly:

```python
from tests._helpers.server_runner import server_runner

with server_runner(tmp_path / "stack", server_env=server_env) as stack:
    stack.start_runner(env=runner_env)
    # Drive the scenario through stack.base_url and stack.runner_id.
```

Environment overrides accept `None` to remove inherited variables. Caller
provider settings, faults, command entry points and compatibility requirements
must survive migration. Do not start an extra stack when a nearby fixture already
provides the required one. Inspect function contracts before extending an API.

## Own user contexts, borrow the browser

Given an existing browser and session URL:

```python
from tests._helpers.browser import user_page

with user_page(browser, headers={"X-Forwarded-Email": "viewer@example.test"}) as page:
    # Install any required WebSocket relay/fault routes before navigating.
    page.goto(session_url)
```

`async_user_page` provides the same ownership with `async with`. Both close the
context even when page creation or the scenario fails, and leave the caller's
browser open. Pass browser context options such as viewport, device profile or
recording directory explicitly. HTTP identity headers do not authenticate a
WebSocket automatically. Preserve transport setup and deliberate disconnect
timing; nested contexts can express a user leaving while another stays online.

## When a primitive does not fit

Name the missing capability before adding setup. Extend a primitive when multiple
actual consumers benefit; keep unique lifecycle behavior local. Keep input
matrices at the lowest layer that exposes the failure, with higher-level coverage
for distinct production boundaries. Real consumers often cover a helper already;
add direct helper tests only for meaningful contract gaps. Measure test/support
LOC including helper growth, and preserve original reproduction evidence when
selecting smaller permanent coverage.

Operator provisioning requires the local server fixture to expose its database
and artifact directory through `operator_agent_store`. It uses the same
registration function as `server --agent`, then creates a session by agent id.
Use it only for operator-authored fixtures; upload validation and authorization
scenarios must keep `post_session_bundle`. External servers are not enrolled.
