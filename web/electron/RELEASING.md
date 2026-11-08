# Desktop release runbook

The desktop version comes from [`package.json`](package.json). Windows and Linux are built and published by the [`omnigent-desktop` workflow](https://github.com/databricks/secure-public-registry-releases-eng/actions/workflows/omnigent-desktop.yml); macOS is built, signed, notarized, and uploaded from a Mac. The workflow opens a PR in [`omnigent-site`](https://github.com/omnigent-ai/omnigent-site) for the Windows/Linux feed. Add macOS to **that same PR** before merging it. Merging the site PR makes the update feed and download redirects live; uploading binaries alone does not.

## 1. Bump and merge the source version

Bump only `web/electron/package.json` on a PR against `omnigent-ai/omnigent` (unless the release also needs other changes). The workspace `pnpm-lock.yaml` does not record this package's own version. Wait for CI and review, then merge. Record the **merge commit SHA** as `SOURCE_SHA` and check out that commit for the Mac build too. Do not build from an older clone or use a different source revision for macOS. Set `VERSION` to the version in the merged manifest (for example, `export VERSION=0.17.2`) and `SOURCE_SHA` to the actual merge commit SHA before running the commands below.

## 2. Build and publish Windows/Linux

Dispatch the secure workflow with the source merge SHA. Its `publish` input defaults to `dry-run`, so select `publish` explicitly for a release:

```bash
gh workflow run omnigent-desktop.yml \
  --repo databricks/secure-public-registry-releases-eng --ref main \
  -f ref="$SOURCE_SHA" -f publish=publish
```

Watch the run's Linux/Windows builds, security scans, Blob upload, and site-PR jobs. The workflow publishes under `win/` and `linux/` in the `omnigent-builds` Vercel Blob store, and opens a PR updating `latest.yml`, `latest-linux.yml`, and the Windows/Linux download redirects. A `dry-run` only previews these writes. Re-dispatching the same version in publish mode is not a retry strategy: uploads are create-only and existing paths cause a collision.

## 3. Build the signed, notarized macOS release

Use a Mac with the **Databricks, Inc. (8RMX4WU6F8)** Developer ID Application certificate and Apple notarization credentials. See [macOS code signing & notarization](README.md#macos-code-signing--notarization) for the supported `APPLE_API_KEY` / `APPLE_API_KEY_ID` / `APPLE_API_ISSUER` (or Apple ID) variables. Keep credentials in ignored local files or a secret manager; never commit, print, or paste them into the PR.

From the source repository root at `SOURCE_SHA` (or a clean checkout containing that exact tree):

```bash
pnpm install --frozen-lockfile --filter ./web --filter ./web/electron
# If the Apple variables are in an ignored web/electron/.env:
set -a; source web/electron/.env; set +a
pnpm --dir web/electron run build:mac:release
```

**Do not use `build:mac` or `just electron-build` for a release.** Those make **Omnigent Dev** in `dist-dev/` and do not notarize. The release command makes **Omnigent** in `web/electron/dist/` for both x64 and arm64. Confirm the signing/notarization steps succeeded:

```bash
for arch in mac mac-arm64; do
  spctl -a -vv "web/electron/dist/$arch/Omnigent.app" # Notarized Developer ID
done
for dmg in web/electron/dist/Omnigent-"$VERSION"-*.dmg; do
  codesign --verify --verbose=2 "$dmg"
  xcrun stapler validate "$dmg"
done
```

**Fix the post-stapling metadata before publishing.** electron-builder generates `latest-mac.yml` and `.dmg.blockmap` files _before_ the DMGs are stapled. Stapling changes their bytes, so the generated DMG hashes/sizes and blockmaps no longer match. From the source repo root, after the release build completes, regenerate the two DMG blockmaps and update the manifest from the final artifacts:

```bash
node <<'JS'
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const yaml = require('./web/electron/node_modules/js-yaml');
const electronBuilder = require.resolve('electron-builder', { paths: ['./web/electron'] });
const { buildBlockMap } = require(require.resolve(
  'app-builder-lib/out/targets/blockmap/blockmap', { paths: [electronBuilder] },
));
const dir = 'web/electron/dist';
const manifestPath = path.join(dir, 'latest-mac.yml');
const manifest = yaml.load(fs.readFileSync(manifestPath, 'utf8'));
(async () => {
  if (manifest.files.length !== 4) throw new Error('Expected two ZIPs and two DMGs');
  for (const entry of manifest.files) {
    const file = path.join(dir, entry.url);
    if (entry.url.endsWith('.dmg')) {
      const info = await buildBlockMap(file, 'gzip', `${file}.blockmap`);
      entry.sha512 = info.sha512;
      entry.size = info.size;
    }
    const bytes = fs.readFileSync(file);
    const hash = crypto.createHash('sha512').update(bytes).digest('base64');
    if (entry.sha512 !== hash || entry.size !== bytes.length) {
      throw new Error(`Manifest mismatch: ${entry.url}`);
    }
  }
  const primary = manifest.files.find(f => f.url === manifest.path);
  if (!primary) throw new Error('Primary artifact missing');
  manifest.sha512 = primary.sha512;
  fs.writeFileSync(manifestPath, yaml.dump(manifest, { lineWidth: -1 }));
})().catch(error => { console.error(error); process.exitCode = 1; });
JS
```

Check that `latest-mac.yml` says the intended `VERSION` and names the production `Omnigent-...` artifacts, **not** `Omnigent Dev-...`. Re-check all four manifest hashes and sizes against the final files if anything changes after this step.

## 4. Upload macOS to Vercel Blob

Use the `omnigent-builds` store's `BLOB_READ_WRITE_TOKEN` (for example, from an ignored root `.env`). The Vercel CLI uses the token when `BLOB_STORE_ID` is unset; setting only the store ID alongside it makes the CLI try incomplete OIDC authentication. Confirm the store is the one backing the existing `mac/` URLs in `omnigent-site`.

```bash
# In a shell at the source repo root; load the token without printing it.
set -a; source .env; set +a
unset BLOB_STORE_ID
vercel blob list --prefix "mac/Omnigent-$VERSION" --limit 100
# Stop if any of this version's paths already exist; do not overwrite a release.
for file in web/electron/dist/Omnigent-"$VERSION"-*.zip \
            web/electron/dist/Omnigent-"$VERSION"-*.dmg \
            web/electron/dist/Omnigent-"$VERSION"-*.blockmap; do
  vercel blob put "$file" --pathname "mac/$(basename "$file")" --access public
done
vercel blob list --prefix "mac/Omnigent-$VERSION" --limit 100
```

Expect **eight** exact paths (two ZIPs, two DMGs, four blockmaps), with the store URL prefix used by the site's existing redirects. **Omit** `--add-random-suffix` and `--allow-overwrite`: the CLI defaults to stable pathnames and create-only uploads; passing `--add-random-suffix false` still enables a random suffix in some CLI versions, breaking the update feed. Check the listed sizes and HTTP availability of the final URLs before editing the site PR.

## 5. Complete the site PR and verify

On the workflow-created `omnigent-site` PR branch, copy the corrected `web/electron/dist/latest-mac.yml` to `public/_desktop/updates/latest-mac.yml`. Repoint `/download/mac` and `/download/mac-x64` to the new arm64/x64 DMGs in the same Blob store; add versioned `/download/mac/v<VERSION>` and `/download/mac-x64/v<VERSION>` routes while preserving the old versioned routes. Keep the Windows/Linux manifest and redirects from the workflow. Update the existing PR description so it no longer says macOS is untouched, then commit and push to **that PR branch**, not `main`.

Run the site repo's `bun run fmt`, `bun run strip-lock-proxy:check`, and `bun run fmt:check` before pushing; verify the PR's CI and Vercel preview. Check that every manifest filename resolves to the exact uploaded Blob pathname, all four manifest hashes/sizes match the final artifacts, and both unversioned macOS redirects point to the intended architecture. Only merge the site PR once these checks pass and the release should go live. Do not re-dispatch the secure workflow after manually updating its PR branch: its site job can force-push that branch and discard the macOS changes.
