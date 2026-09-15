# Collaboration and access

## Sub-features

- Share link and workspace/public access toggle
- User grants, read/edit/manage levels, approval delegation, and revoke
- Presence, realtime messages, ownership, anti-enumeration, and attribution
- Accounts/header auth in OSS; request-context and WHS identities downstream

## How to get to it

Open a session as its owner, choose **Share session**, grant another identity,
and open the same link as that collaborator. Change or revoke access from the
same modal.

## Driving it with Playwright

Run `scripts/verify.sh collaboration`. Drive **Share session** and the
permissions dialog with separate browser contexts. At each level assert both
the visible composer, read-only, or no-access state and the REST authorization
result: ungranted or revoked users receive anti-enumerating 404s, readers cannot
post, and editors can post only where policy permits.

The profile requires the permissions and sharing journeys plus
`tests/e2e_ui/collaboration/test_collab_realtime.py` and
`tests/e2e_ui/collaboration/test_author_label.py`. Evidence preparation fails
if any required journey is omitted from the command plan.

The observable end state is agreement among `/v1/info`, visible controls,
permission-store state, and server enforcement.

Changes to `omnigent/server/routes/sessions/routes_core.py` or
`routes_hooks.py` select this lane because those exact modules enforce session
ownership, collaborator reads/writes, and permission-request authorization.
Other files named `core` or `hooks` do not select collaboration by name alone.

## Gotchas

- Permission changes may require collaborator reload by design.
- Single-user mode hides Share; sharing-off multi-user mode disables it with an
  explanation.
- Databricks `restricted_read_only` can also prohibit sharing sessions rooted at
  home or filesystem root.
- WHS workspace-wide access is a group grant, not OSS's `__public__` sentinel.
- Treat auth or identity changes as downstream-sensitive even when OSS tests pass.
