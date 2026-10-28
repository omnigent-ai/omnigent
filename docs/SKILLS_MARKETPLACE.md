# Skills marketplace

Settings → **Skills** lets a user browse git repositories of skills, install
the ones they want, and have them offered in the slash menu of every new
session, on any connected host or managed sandbox. Administrators choose which
repositories are browsable; anyone can preview a GitHub repository by URL.

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
repositories are supported, written as `owner/repo` or a `github.com` URL,
optionally at a branch or tag (`/tree/<ref>`, or the ref field in the UI).

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
- Anyone can preview a repository by URL and install from it for themselves.

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
| `POST /skill-catalog` | Index any GitHub repository (the Add-from-URL path) |
| `GET/POST /installed-skills` | List (paged with `limit` and `after`), and install by name from a repository |
| `PATCH/DELETE /installed-skills/{id}` | Enable or disable, and remove |
| `POST /installed-skills/{id}/update` | Refetch at the same ref |
| `GET /installed-skills/{id}/content` | The stored `SKILL.md` |

`GET /v1/skills` lists installed skills alongside bundled and host skills, with
a `source` field (`owner/repo`) so clients can label them.
