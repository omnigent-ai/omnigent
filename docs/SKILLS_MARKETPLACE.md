# Skills marketplace

Settings → **Skills** lets a user browse git repositories of skills, install
the ones they want, and have them offered in the slash menu of every new
session, on any connected host or managed sandbox. Administrators choose which
repositories are browsable; anyone can preview a GitHub repository by URL,
unless the [operator policy](#operator-policy) narrows that.

The feature is off by default. Enable it with:

```sh
OMNIGENT_FEATURES=skills_marketplace
```

## What a repository needs

Two layouts are read:

- **A plain skills repository**: `skills/<name>/SKILL.md` at the root, or the
  portable `.agents/skills/` and Claude Code `.claude/skills/` layouts. A
  directory one level down without its own `SKILL.md` is a namespace folder.
- **A Claude Code marketplace**: `.claude-plugin/marketplace.json` listing
  plugins. Each plugin whose `source` is a path inside the repository
  contributes its `skills/`. A plugin that points at another repository is
  listed as skipped rather than followed.

A root-level skill shadows a plugin skill of the same name. Only GitHub
repositories are supported, written as `owner/repo`, a `github.com` URL, or a
URL on a GitHub Enterprise Server host the server knows (`GH_HOST`,
`OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS`, or a host `gh` is signed in to),
optionally at a branch or tag (`/tree/<ref>`, or the ref field in the UI). An
Enterprise host is kept all the way to the clone URL and shown as
`host/owner/repo`; a URL on an unknown host is refused, never rewritten onto
github.com.

## How a skill reaches a session

An installed skill is stored server-side as a tarball of its directory, in the
artifact store. When a runner fetches a session's agent bundle, the server
adds the session owner's enabled skills under the bundle's `skills/` folder,
so every harness discovers them exactly like skills the agent author bundled.
The agent spec's `skills:` filter applies to them as it does to host skills,
and a skill the bundle itself carries always wins a name collision.

Sessions pick up installs, updates and removals when they next start. With
the feature switched off, installed rows stay in the database but nothing
reads them: the routes are absent and neither discovery nor served bundles
include them.

## Scope and permissions

- A user's installs are visible only to them. An administrator can install
  for **Everyone**, which every user receives. When a user and the org both
  have a skill of the same name, the user's copy wins for that user.
- Only administrators add or remove browsable sources. Deleting a source does
  not remove skills already installed from it.
- Anyone can preview a repository by URL and install from it for themselves,
  unless `allow_url_install` is off.

## Operator policy

Admin is a property of a user. The `skills_marketplace:` block of the server
config (`-c server_config.yaml`, or `<data_dir>/config.yaml`; see
[POLICIES.md](POLICIES.md)) is a constraint set by whoever provisions the
server, and it holds for administrators and the single local user alike. It is
enforced on every fetch path (`POST /skill-sources`, `POST /skill-catalog`,
`POST /installed-skills` and `/update`) and again when a session's bundle is
served, so tightening it also stops serving skills installed under a looser
policy. With no block, behaviour stays as described above.

```yaml
skills_marketplace:
  sources:                 # seeded at startup, read-only in the UI
    - url: https://github.com/acme/skills
      ref: v1.4.0
      name: Acme skills    # optional
  allowed_sources:         # host/owner/repo globs; empty = any
    - github.com/acme/*
    - github.acme.internal/*/*
  allow_url_install: false
  credentials:
    - match: github.acme.internal/*/*
      token_env: SKILLS_GHE_TOKEN
```

- `sources` are added to the browse list when the server starts (an existing
  row is kept) and show a **Config** tag; nobody can remove them from Settings.
  Removing one from the file leaves the row behind as an ordinary source.
- `allowed_sources` lists `host/owner/repo` patterns; `*` matches within one
  segment, and a shorter pattern such as `github.acme.internal` covers the
  rest. A repository outside the list cannot be added, previewed, installed
  or updated, and an installed skill from one is no longer served.
- `allow_url_install: false` removes **Add from URL** and refuses installs
  (and updates) from any repository that is not a listed source.
- `credentials` let the server reach private repositories without borrowing
  the operator's own git setup: the token named by `token_env` is read from
  the environment at fetch time and sent only to a repository that matches
  both `allowed_sources` and `match`, as an authorization header through
  `GIT_CONFIG_COUNT` (git 2.31 or newer), never on the command line. The
  file itself never holds a secret.

`GET /v1/info` reports `skills_marketplace.url_install` so the Settings page
can hide the URL form.

## Safety

Repositories are fetched the way `omnigent agent add` fetches them: a shallow
clone with git hooks, submodules, credential prompts and LFS disabled, and the
checkout is packed without following symlinks and without expanding `${VAR}`
references. A skill is markdown and supporting files the harness reads; it
runs with whatever the harness already allows. Every card and installed entry
shows the repository and the commit it was taken from.

The server's clone is isolated from its own git configuration and credential
helpers, so a user cannot reach a private repository through the operator's
credentials. Limits are checked while packing, before more is read: at most
20 skills per request, 20 MB and 2,000 files per skill, 100 MB per request,
and two fetches at a time. Repository metadata that would not fit its column
is rejected before anything is stored.

## API

Under `/v1`, mounted only when the feature is on:

| Route | Purpose |
| --- | --- |
| `GET/POST/DELETE /skill-sources` | Browsable repositories, paged (POST and DELETE are admin-only) |
| `GET /skill-sources/{id}/catalog` | The skills a source offers; `?refresh=true` re-fetches |
| `POST /skill-catalog` | Index any GitHub repository (the Add-from-URL path; a listed source only when `allow_url_install` is off) |
| `GET/POST /installed-skills` | List (paged with `limit` and `after`), and install by name from a repository |
| `PATCH/DELETE /installed-skills/{id}` | Enable or disable, and remove |
| `POST /installed-skills/{id}/update` | Refetch at the same ref |
| `GET /installed-skills/{id}/content` | The stored `SKILL.md` |

`GET /v1/skills` lists installed skills alongside bundled and host skills, with
a `source` field (`owner/repo`) so clients can label them.
