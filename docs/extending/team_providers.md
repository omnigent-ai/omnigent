# External team membership

This prototype reads teams from a customer-owned directory. Omnigent passes its
existing authenticated user ID to one installed provider; it does not create
users or groups, synchronize memberships, or persist directory credentials/data.
Authentication and sandbox access rules are unchanged.

## Try the reference providers

Install the optional example package into the **server's** Python environment:

```sh
uv pip install --no-deps ./examples/team-providers
```

Select a provider and supply its credentials through your deployment's existing
secret mechanism before starting the server. The example package exposes:

| Provider selection | Required environment variables | Directory identity |
| --- | --- | --- |
| `OMNIGENT_TEAM_PROVIDER=okta` | `OMNIGENT_OKTA_ORG_URL`, `OMNIGENT_OKTA_API_TOKEN` | Full Okta login or immutable user ID |
| `OMNIGENT_TEAM_PROVIDER=google` | `OMNIGENT_GOOGLE_GROUPS_ACCESS_TOKEN` | Google Workspace email or immutable user ID |

For Okta, use the HTTPS organization origin, such as
`https://your-org.okta.com`, with no path. The token's associated account must be
allowed to read users' groups. This example uses an Okta API token; a customer
plugin can instead use its existing OAuth credential provider.

For Google, enable the Admin SDK Directory API and externally obtain an OAuth
access token with `https://www.googleapis.com/auth/admin.directory.group.readonly`
and the required directory privileges. Service-account delegation, consent,
refresh, and rotation stay with your existing identity tooling. The examples
read credentials at server startup; restart after replacing a token.

Use a verified, trusted authentication source whose user IDs identify the same
directory users. The examples pass IDs unchanged; they do not link an arbitrary
local username to a directory account or verify ownership of a claimed email.
If your identities differ, translate them in your customer plugin using your
existing identity system. Never expose trusted-header authentication directly
to untrusted clients.

## Query your memberships

Use your normal authenticated server request, for example an exported session
cookie jar:

```sh
curl --fail-with-body --cookie cookie.txt "$OMNI_SERVER/v1/me/teams"
curl --fail-with-body --cookie cookie.txt --get \
  --data-urlencode 'team_id=00g-example' "$OMNI_SERVER/v1/me/team-membership"
```

Example responses:

```json
{"teams": [{"id": "00g-example", "name": "Engineering"}]}
```

```json
{"team_id": "00g-example", "member": true}
```

These endpoints only query the signed-in user. A `user_id` query parameter does
not select somebody else. Membership uses the provider's stable team ID, never
its display name. Each request reads the directory again, with a 15-second
total timeout and no local membership cache. No authenticated identity returns
401; an unconfigured, failing, or timed-out provider returns 503. Only a
successful complete lookup can return an empty list or `member: false`.

## Add another provider

An installed Python package implements one async method:

```python
from omnigent.team_providers import Team, TeamProvider


class CustomerTeams(TeamProvider):
    async def list_teams(self, user_id: str) -> list[Team]:
        groups = await existing_directory_client.groups_for(user_id)
        return [Team(id=group.id, name=group.name) for group in groups]
```

Register the class or a zero-argument factory in that package's `pyproject.toml`:

```toml
[project.entry-points."omnigent.team_providers"]
customer = "customer_teams:CustomerTeams"
```

Set `OMNIGENT_TEAM_PROVIDER=customer` and restart the server. An explicit
selection must resolve to exactly one installed provider and initialize
successfully; configuration errors stop startup. Unset selection leaves the
feature unavailable. One provider is selected per server process.

Return the complete list or raise an exception. Avoid blocking the event loop
and never turn a provider error into an empty list. Membership checks reuse this
list, so no second plugin method is required. Provider-specific nested-group
semantics remain with the provider; the examples add no transitive expansion.

Okta uses its user-groups endpoint. Google follows `groups.list` pagination;
more than 25 pages produces an error, not a truncated result. These examples
are installable independently and add no dependencies to the default server.

## Verify the prototype

```sh
.venv/bin/pytest tests/test_team_providers.py \
  tests/test_team_provider_examples.py tests/server/routes/test_teams.py -q
```

The checks use real plugin discovery/route code with mocked directory responses,
including authorization, empty membership, directory failures, and Google
pagination. They do not contact a live customer directory. With real credentials,
compare `/v1/me/teams` against your directory's memberships, check a known team
ID and a missing one, then change membership in the directory and query again.
