# Git providers

Git providers recognize repository and pull request URLs without importing a
server, SDK, or network client. The descriptor preserves the provider ID, host,
full repository path, request number, and canonical URL. Existing stored
references without a provider ID continue to mean GitHub.

## Host upgrade and rollback

The legacy session registry remains GitHub-only. Other providers' associations
live in a sibling `*.providers.json` file, coordinated by the same per-session
lock. Reading a mixed registry from an earlier prerelease migrates it to this
layout without dropping titles, removal history, or replay protection.
Foreign removals save their exclusion first, and reads suppress excluded
associations. An interruption between file replacements therefore cannot let
discovery restore an unlinked request. Migration and explicit reattachment
still write the companion first.

An older host sees and updates only GitHub associations. Other providers remain
saved but unavailable until the host is upgraded again; old-host GitHub edits
and removals are preserved. Removal and observation history stay in the legacy
file because older hosts already preserve those opaque values. This is local
host downgrade protection, separate from server/host wire compatibility.

## Add an installed provider

Install a Python package in the Omnigent host environment with an entry point:

```toml
[project.entry-points."omnigent.git_providers"]
example = "example_forge:PROVIDER"
```

The entry point exposes a descriptor object or a zero-argument factory returning
one. Use the `GitProvider` protocol in `omnigent.git_providers` as the contract:

- A stable lowercase `id`, a `display_name`, and `default_hosts`.
- `matches_host(host, instances)`, `parse_remote_url(url, instances)`, and
  `parse_pr_url(url, instances)`.
- A `FacetModules` value containing optional module paths. Only a caller that
  needs a facet imports it; unset facets mean that capability is unavailable.

Descriptors should use only the standard library and perform no network calls.
A provider can supply `request_name` (such as `"merge request"`) and
`number_prefix` (such as `"!"`) for shared UI labels. The defaults are
`"pull request"` and `"#"`.

Discovery runs once per process. Restart Omnigent after installing a provider.
Built-in IDs cannot be overridden. Duplicate IDs, invalid descriptors, and
broken imports are logged and skipped so other providers remain available.

## Host configuration and identity

`OMNIGENT_GIT_PROVIDER_<ID>_HOSTS` supplies comma-separated configured instances.
Providers decide how those instances map to their URLs and authentication.
GitHub also recognizes `GH_HOST` and the signed-in hosts in the gh CLI config.
Existing GitHub Enterprise pull request URLs retain their parsing behavior.

Treat URLs from tools and repositories as untrusted input: reject credentials
in request URLs, preserve the full repository path, and validate a destination
before sending credentials. Resolve authentication at the provider boundary;
URL parsing and observation must not fetch credentials or make network calls.
