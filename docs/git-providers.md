# Git providers

Git providers recognize repository and pull request URLs without importing a
server, SDK, or network client. The descriptor preserves the provider ID, host,
full repository path, request number, and canonical URL. Existing stored
references without a provider ID continue to mean GitHub.

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

Keep descriptor imports lightweight and perform no network calls. Load local
configuration parsers only when reading the CLI's saved hosts.
A provider can supply `request_name` (such as `"merge request"`) and
`number_prefix` (such as `"!"`) for shared UI labels. The defaults are
`"pull request"` and `"#"`.

Discovery runs once per process. Restart Omnigent after installing a provider.
Built-in IDs cannot be overridden. Duplicate IDs, invalid descriptors, and
broken imports are logged and skipped so other providers remain available.

## Host configuration and identity

The session panel discovers requests from every provider represented by the
checkout's remotes. A tracked request from one provider does not disable the
others. Selecting a particular request reads that request independently of the
checkout, and removing a request prevents discovery from adding it again.

Providers should recognize their public hosts and the instances in their CLI's
existing login configuration without requiring Omnigent settings.
`OMNIGENT_GIT_PROVIDER_<ID>_HOSTS` optionally adds comma-separated instances.
Providers decide how those instances map to their URLs and authentication.
GitHub also recognizes `GH_HOST` and the signed-in hosts in the gh CLI config.
Existing GitHub Enterprise pull request URLs retain their parsing behavior.

Treat URLs from tools and repositories as untrusted input: reject credentials
in request URLs, preserve the full repository path, and validate a destination
before sending credentials. Resolve authentication at the provider boundary;
URL parsing and observation must not fetch credentials or make network calls.
