# Publishing the Omnigent VS Code extension

This directory holds the Omnigent VS Code extension. It is published
under the shared **`databricks`** VS Code Marketplace publisher (and the
`databricks` Open VSX namespace), which means releases flow through the
Databricks security-hardened release path — direct publishing from this repo is
blocked by policy.

The work splits into two halves:

- **This repo** builds a SHA256-verified `.vsix` and attaches it to a GitHub
release. No marketplace tokens live here.
- **The secure-release repo**
([`databricks/secure-public-registry-releases-eng`](https://github.com/databricks/secure-public-registry-releases-eng))
downloads that `.vsix`, verifies the checksum, scans it (`databricks/gh-action-scan`),
and publishes to the VS Code Marketplace and Open VSX. It holds the tokens and
the approval gate.

Two existing workflows in the secure repo are the reference:
[`databricks-vscode.yml`](https://github.com/databricks/secure-public-registry-releases-eng/blob/main/.github/workflows/databricks-vscode.yml)
(the extension publish flow to adapt) and
[`omnigent.yml`](https://github.com/databricks/secure-public-registry-releases-eng/blob/main/.github/workflows/omnigent.yml)
(omnigent's PyPI release, which already checks out `omnigent-ai/omnigent`
cross-org). Both require SAML SSO to view.

## Steps to release

No tags are pushed by hand — the version flows from a reviewed PR into the
`.vsix` and the release tag, so they can't diverge.

1. **Open the release PR.** Run the **VS Code Extension Release PR** workflow
   (`vscode-release-pr.yml`) with the target version (e.g. `0.2.0`). It bumps
   `editors/vscode/package.json`, adds a `CHANGELOG.md` section, and opens a
   `Release (vscode): v0.2.0` PR. Review and merge it.
2. **Build the draft release.** Run the **VS Code Extension Release** workflow
   (`vscode-extension-release.yml`). It reads the version from `package.json`,
   builds the `.vsix`, attaches it and its `.sha256`, and creates a **draft**
   `vscode-v<version>` release (a dedicated tag namespace kept separate from the
   Python release tags `v[0-9]*`).
3. **Publish the draft.** The workflow leaves the release as a draft: it is not
   public and the `vscode-v<version>` git tag is not created until you publish.
   On GitHub, open the repo's **Releases** page, find the draft, confirm the
   attached `.vsix` + `.sha256` and the notes look right, then click **Publish
   release**. Publishing creates the tag and makes the release downloadable by
   the secure-repo workflow.
4. **Smoke-test the `.vsix` locally.** Download the `.vsix` from the published
   release and install it into a clean VS Code, then confirm the extension
   activates and opens a local server:

   ```bash
   gh release download vscode-v<version> --repo omnigent-ai/omnigent --pattern '*.vsix'
   code --install-extension omnigent-vscode-<version>.vsix
   ```

   Reload VS Code, start a local server (`omnigent server`), and run the
   **Omnigent: Open** command — confirm it opens an editor pane showing the
   running server's UI (not a blank pane or an error). This catches packaging
   problems (missing files, a broken bundle) before anything reaches the
   marketplaces.
5. **Publish to the marketplaces.** Dispatch `omnigent-vscode.yml` in the
   secure-release repo (once it exists), pointing at the `vscode-v<version>`
   tag; run with `dry-run: true` first, then publish for real.

The one-time setup that makes this possible is tracked below.

## Setup (one-time)


| #   | Step                                                                                                                                                                                                                                                                                                                                           | Where                                                                                                                                                                       | Blocked on          |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| 1   | Set `"publisher": "databricks"` in `package.json`                                                                                                                                                                                                                                                                                              | `editors/vscode`                                                                                                                                                            | — (done)            |
| 2   | Maintain `CHANGELOG.md` (strip Jira refs, keep GH issue refs)                                                                                                                                                                                                                                                                                  | `editors/vscode`                                                                                                                                                            | — (done)            |
| 3   | Verify the build: `npm ci && npm run build && npm run package` → valid `.vsix`                                                                                                                                                                                                                                                                 | local / CI                                                                                                                                                                  | — (done)            |
| 4   | Release-PR workflow bumps version + CHANGELOG; a manually-dispatched release workflow builds the `.vsix` and attaches it (+`.sha256`) to a draft GitHub release                                                                                                                                                                                | `.github/workflows/vscode-release-pr.yml`, `vscode-extension-release.yml`                                                                                                   | — (done)            |
| 5   | Ask DECO to register `omnigent-vscode` under the `databricks` publisher + issue a Marketplace PAT                                                                                                                                                                                                                                              | Slack `#dev-ecosystem-discuss` ([https://databricks.slack.com/archives/C01KSAWFXG8/p1782971196701749](https://databricks.slack.com/archives/C01KSAWFXG8/p1782971196701749)) | human approval      |
| 6   | Add an `omnigent-vscode.yml` publish workflow in the secure repo, adapting the existing [`databricks-vscode.yml`](https://github.com/databricks/secure-public-registry-releases-eng/blob/main/.github/workflows/databricks-vscode.yml) (SAML SSO required) — it already does download → scan → `vsce publish` + `ovsx publish` in one workflow | `secure-public-registry-releases-eng`                                                                                                                                       | DECO grant (step 5) |
| 7   | Confirm `VSCE_TOKEN` + `OVSX_PAT` cover the omnigent publisher (the `databricks-vscode-marketplace` environment already holds them); register rows in `go/npp-release-status`; get sign-off in `#unblock-releases-public`                                                                                                                      | secure repo + Slack                                                                                                                                                         | steps 5–6           |


Steps 1–4 are complete in this repo. Steps 5–7 need the DECO grant and the
secure-repo reference workflow.

## Notes for the secure-repo workflow

- `databricks-vscode.yml` downloads the `.vsix` from the release with
`gh release download -R databricks/databricks-vscode`. The omnigent variant
must point `-R` at `omnigent-ai/omnigent` and relax the filename check (it
hard-codes `databricks-*-${VERSION}.vsix`; ours is `omnigent-vscode-*.vsix`).
- We author `omnigent-vscode.yml` ourselves — it's our own pipeline, not a fork
of `databricks-vscode.yml`, so we just write it to expect our `vscode-v*`
tags. `databricks-vscode.yml` is only a structural template; its
`Validate tag format` step hard-codes `^release-v([0-9]+\.[0-9]+\.[0-9]+)$`,
which is *their* convention — when copying that step, change the regex to
match `vscode-v*`. The only thing that must agree is our two ends
(`vscode-extension-release.yml` and `omnigent-vscode.yml`), and we control
both.
- Cross-org fetch from the public `omnigent-ai/omnigent` repo is already an
accepted pattern — `omnigent.yml` checks out `omnigent-ai/omnigent` for the
PyPI release. So reading a public omnigent release from the hardened runner is
not a new blocker.
- Marketplace publish binds the `databricks-vscode-marketplace` environment and
reads `VSCE_TOKEN` / `OVSX_PAT`. A new omnigent flow either reuses that
environment or gets its own (confirm with DECO which, per step 7).

