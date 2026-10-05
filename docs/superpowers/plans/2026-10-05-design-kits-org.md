# Design page: kits per user and per organization

Contract: `### Kits per user or organization` under `## Later phases` in
`designs/DESIGN_PAGE.md`. Branch `polly/design-kits-org`, on the standalone
HTML export (design-system import, deck index, design systems below it).

## Findings that shape the plan

- The project order preference is the model: `SqlPreference` rows keyed by
  `(workspace_id, user_id, key)`, read and upserted by
  `SqlAlchemyProjectStore` with named sessions (`read_project_order`,
  `save_project_order`), `RESERVED_USER_LOCAL` in single-user mode,
  `require_active_account` on writes, and a decode that falls back to the
  default on a corrupt row. The design default lives in the same store under
  key `design_default` and shares the dialect upsert, so account deletion
  already drops it.
- The routes go on the existing design router (`routes/design.py`), gated by
  the `design` flag like the deck index, with `require_user` for auth. The
  router gains the project store and the org kit snapshot.
- The stored value is tri-state, so "none" can beat the org kit:
  `null` (unset), `{"kind": "none"}`, or
  `{"kind": "full" | "skill", "host_id", "path", "name"}`. PUT validates it
  with the same path rules as the `.omnigent/design-system.json` pointer
  (no NUL, no `.` or `..` segments) and the 80-character name cap.
- Branding resolves assets under `{config_dir}/branding-assets/` with
  `_resolve_branding_asset` (no absolute or `..` names, no symlinked
  directory or path component, must resolve inside the root). Its
  confinement part becomes a shared helper so the kit reuses it instead of
  a second copy. The snapshot is loaded once in `create_app`, like branding.
- `design_kit: true` turns the kit on; the folder is always
  `{config_dir}/design-kit/`, so a config value can never point the asset
  route at the config file or its secrets. Every regular file below it must
  pass the shared confinement check and have a kit extension (`.json`,
  `.css`, the kit font and image extensions the viewer allows); other files
  are skipped and not served. A symlink anywhere, a file over 2 MB, more
  than 20 MB in total, or a `kit.json` without a string `name` disables the
  kit with a warning, since a partial kit renders wrong.
- No directory listing: the web reads the kit's `kit.json` with
  `parseDesignKit` and copies exactly the files the viewer would load
  (`kit.json`, `css`, `logo.src`, `fonts.*.src`). The asset route serves only
  paths in the validated set, with the kit's content type, `nosniff`, and a
  `default-src 'none'; sandbox` CSP so an SVG opened directly runs nothing.
- Materialization reuses `writeFileContent(..., "base64")` and the
  design-system caps (`DS_ASSET_MAX_BYTES`, `DS_DECK_MAX_BYTES`). `kit.json`
  is written last, so a half-finished copy never reads as a kit.
- Precedence is one pure function, `defaultDesignChoice`, used once by the
  dialog: explicit choice, folder kit, user default (when its host is the
  selected host; a stored "none" stops here), org kit (only when the folder
  has no kit), none. The phase 3 "most recent system" default is removed;
  recents stay as options.

## Tasks (tests first, one commit each)

1. **Preference store.** `get_design_default` / `save_design_default` on
   `ProjectStore`, sessions `read_design_default` / `save_design_default`,
   shared upsert with the project order. Tests: unset, round trip, clear,
   per-user isolation, corrupt row reads as unset.
2. **Preference routes.** `GET` / `PUT /v1/me/preferences/design-default` on
   the design router with request and response schemas. Tests: auth
   required, round trip, "none", clear, validation (bad kind, missing
   field, `..` path, long name, NUL), 404 with the flag off.
3. **Org kit config.** `load_design_kit_snapshot` in `server_config.py` on
   the shared confinement helper. Tests: unset, valid kit, escape via
   symlinked file and directory, nested symlink, oversize file, total cap,
   missing or nameless `kit.json`, skipped extensions.
4. **Info and asset route.** `design_kit: {name} | null` in `GET /v1/info`,
   `GET /v1/design-kit/{path}` on the design router. Tests: info set and
   unset, auth required, served bytes and content type, unknown path and
   directory 404, traversal 404, flag off 404. Regenerate `openapi.json`.
5. **Web API.** `designDefault` read and save, org kit name from server
   info, `materializeOrgKit` in `designDeckApi.ts`. Tests: request shapes,
   404 as unset, copy order with `kit.json` last, size caps, fetch failure.
6. **Dialog.** `defaultDesignChoice` in `designSystem.ts`, the org kit
   option, "Make this my default", and materialization on Create. Tests:
   every precedence step, default from another host ignored, stored none
   beats the org kit, the checkbox saves, org kit copied before the first
   message.
7. **Docs.** Operator docs for `design_kit:` (deploy README, config
   example), feature map, and the Design page spec status.

## Out of scope

Wireframes, brand-rule warnings, an admin upload API, server-side
`kit.json` schema validation beyond `name`.
