# Omnigent Company Brain

This package is the Pilot v1 connector and publication plane for wulo-work. It connects
admin-selected Google Workspace, Slack, and Notion resources, writes deterministic Markdown to a
customer-owned Git repository, then updates one isolated gbrain index. Agent bundles opt in with
`company_brain: true`; the MCP URL and bearer never enter the bundle.

## Pinned Runtime

The tested gbrain version is `v0.46.30.0` at commit
`872c3d6ae4073eb6e77c661d0a72f30b31c4c999`. The production image installs that immutable commit;
manual hosts must do the same:

```bash
bun install -g github:garrytan/gbrain#872c3d6ae4073eb6e77c661d0a72f30b31c4c999
gbrain --version
```

The machine must report `gbrain 0.46.30.0`. The executable on npm is unrelated and must not be
used. The machine-readable contract is in `gbrain-compatibility.json`.

## Pilot Topology

Run one stack per pilot company. Each stack owns:

- one Omnigent database and deployment;
- one private Git brain repository;
- one gbrain database and `GBRAIN_HOME`;
- one HTTPS MCP endpoint and read-only agent credential;
- one independent set of Google, Slack, and Notion OAuth clients.

Do not enable this Pilot implementation in a shared multi-company deployment. Workspace columns are
already present, but per-workspace gbrain provisioning and token rotation remain productization work.

## Source Transform Profiles

The Google picker accepts Shared Drives, folders, Docs, Sheets, Slides, PDF, DOCX, XLSX, PPTX, and
organization-shared calendars. Native Docs use the Drive Markdown export. Sheets render a stable
table capped at 500 rows and 50 columns while retaining the complete CSV as provenance. Slides keep
source slide order. PDF and OpenXML files use pinned MarkItDown conversion and are limited to 25 MiB
per source file. Slack emits one stable page per public-channel thread. Notion recursively renders
the blocks beneath roots explicitly shared with the integration.

Small JSON provenance is committed under `.raw/`. Sheet CSV and binary originals are written to the
configured Omnigent artifact store under content-addressed keys; Git contains the corresponding
hash, media type, size, and artifact-key pointer. A missing artifact store, a hash mismatch, an
unsupported type, or an oversized file fails the fetch before reconciliation, so an incomplete run
cannot publish deletions.

## Initialize gbrain

For an always-on HTTP MCP pilot, use a dedicated Postgres 16 database with pgvector. PGLite is
suitable for local tests and offline demos, but `gbrain serve --http` cannot delegate concurrent
`gbrain sync` calls to its single writer. Create the database, then initialize it before the first
Omnigent sync:

```bash
export GBRAIN_HOME=/srv/wulo/company-brain/gbrain
export GBRAIN_DATABASE_URL='postgresql://.../company_brain'
gbrain init --url "$GBRAIN_DATABASE_URL" --non-interactive
gbrain doctor
```

Start HTTP MCP with the included entrypoint:

```bash
export GBRAIN_PUBLIC_URL=https://brain.example.com
export GBRAIN_ADMIN_BOOTSTRAP_TOKEN='set-through-your-secret-manager'
sh integrations/company_brain/scripts/serve-gbrain.sh
```

Terminate TLS at the deployment ingress and route `GBRAIN_PUBLIC_URL` to loopback port `3131`. Verify
`https://brain.example.com/health` before registering the agent client.

For the bundled Docker deployment, bootstrap and start the opt-in profile:

```bash
cd deploy/docker
./bootstrap.sh --company-brain
```

Set `GBRAIN_PUBLIC_URL`, `OMNIGENT_COMPANY_BRAIN_MCP_URL`,
`OMNIGENT_COMPANY_BRAIN_REPO_URL`, `GIT_TOKEN`, and the OAuth client values for each enabled source
in `.env`, then run:

```bash
docker compose --profile company-brain up -d --build
docker compose --profile company-brain ps
docker compose --profile company-brain exec gbrain gbrain doctor
docker compose --profile company-brain exec gbrain gbrain sources status --json
```

The profile uses a digest-pinned pgvector 0.8.1/Postgres 16 image and separate durable database
volume. Omnigent and the gbrain HTTP process share only the Company Brain Git and gbrain state
volume. The published Omnigent server image contains the tested Bun 1.2.23 and gbrain 0.46.30.0
runtime. The Git credential helper reads `GIT_TOKEN` and `GIT_USERNAME` from process environment and
does not write the token into the repository remote URL. The profile defaults to deterministic
keyword retrieval with `OMNIGENT_COMPANY_BRAIN_NO_EMBEDDING=1`; set it to `0` only after configuring
an embedding provider and dimensions accepted by `gbrain doctor`.

## Provision Read-only Agent Access

Run this on the brain host after the `company-shared` source has been created by the first sync:

```bash
gbrain agent register wulo-work \
  --harness claude-code \
  --preset daily-driver \
  --source company-shared \
  --federated-read company-shared \
  --scopes read \
  --surface starter \
  --url "$GBRAIN_PUBLIC_URL/mcp" \
  --show-token \
  --json
```

Store the resulting bearer in the deployment secret manager as
`OMNIGENT_COMPANY_BRAIN_MCP_TOKEN`. Never place it in an agent bundle, database row, Git repository,
or checked-in environment file. Set `OMNIGENT_COMPANY_BRAIN_MCP_URL` to the HTTPS `/mcp` URL. For
Docker, update `.env` after registration and recreate only the API container with
`docker compose --profile company-brain up -d --no-deps --force-recreate omnigent`. Rotate the
client before its token expires and recreate Omnigent so new runner handoffs use the new token.
The server entrypoint suppresses gbrain's bootstrap token in logs; the agent bearer above is a
separate read-scoped credential created explicitly after the first source exists.

## Configure Omnigent

Copy `.env.example` into the deployment's secret configuration and fill every required value. The
three OAuth callback URLs are:

```text
https://APP_HOST/v1/company-brain/oauth/google/callback
https://APP_HOST/v1/company-brain/oauth/slack/callback
https://APP_HOST/v1/company-brain/oauth/notion/callback
```

The configured repository path must be a private repository the customer owns or can clone. Add its
authenticated `origin` remote and set `OMNIGENT_COMPANY_BRAIN_GIT_PUSH=1`; publication then treats a
successful push as part of the sync transaction. A failed push is retryable without creating a
duplicate commit.

## Agent Opt-in

Any user-created native Omnigent agent can attach the managed read-only brain:

```yaml
spec_version: 1
name: policy-analyst
company_brain: true
executor:
  config:
    harness: claude-sdk
```

At runtime the authenticated runner receives the managed MCP URL and bearer through its bound
control-plane response. The user-authored bundle remains unchanged. The exposed gbrain operations
are limited to retrieval, cited synthesis, and graph traversal.

## Pilot Verification

1. Connect one provider in Settings > Company brain.
2. Select organization-shared resources and inspect up to five transformed pages.
3. Activate and confirm an atomic commit appears in the private repository.
4. Run `gbrain sources status --json` and `gbrain doctor` on the brain host; require a recent
  `last_sync_at`, zero queue depth, and no active sync for `company-shared`.
5. Create an agent with `company_brain: true` and ask a question whose answer spans source pages.
6. Confirm the answer cites the canonical source URLs.
7. Edit or explicitly archive a source item, run Sync now, and verify its citation no longer appears
  in normal search while the deletion tombstone remains available only with `--include-deleted`.
8. Disconnect the provider and verify historical Git content and indexed knowledge remain.

## Recorded Pilot Debt

- One company per deployment; no shared multi-company gbrain fleet.
- OAuth client secrets and the credential-encryption key are deployment environment secrets.
- One active credential-encryption key; rotation tooling is deferred.
- Bounded full re-fetches instead of provider cursors or webhooks.
- PDF and Office conversion is capped at 25 MiB per file; larger files need a productized async path.
- Process-local orchestration serialization; deploy one Omnigent process for the pilot.
- Minimal sync activity rather than the productized run-detail drawer.
- No load test or automated company provisioning.