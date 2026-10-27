# Skills marketplace

Settings → Skills lets a user browse repositories of skills, install the ones they want, and have them offered in every new session's slash menu on any host or sandbox. Administrators manage which repositories are browsable; anyone can preview a GitHub repository by URL. Off by default, behind the `skills_marketplace` feature.

## Sub-features

- `skill-sources`: administrators add a GitHub repository (`owner/repo` or URL, optional branch or tag) to the browse list and remove it again; removing a source leaves installed skills in place.
- `skill-catalog`: a source's skills, with name, description, plugin tag for marketplace plugins, path and commit, searchable; skills the user already installed show Installed instead of a button; entries the marketplace could not read are listed as skipped.
- `skill-preview-url`: previewing any GitHub repository by URL shows its catalog without adding it as a source, and installs from it the same way.
- `skill-install-scope`: a user installs for themselves; an administrator can install for everyone.
- `installed-skills`: the Installed tab lists each skill with its source, ref, commit and scope, with enable/disable, Update (refetch at the same ref), Remove behind a confirmation, and a SKILL.md preview.
- `skills-in-session`: an enabled installed skill appears in the new-session slash menu and is usable in the session, on a connected host and in a managed sandbox, for every harness; the agent spec's `skills:` filter still applies and a bundled skill of the same name wins.

## How to get to it (user POV)

- Settings → Skills → Browse: pick a source on the left, search, Install on a card; or paste a repository under Add from URL and Preview skills.
- Settings → Skills → Installed: toggle, Update, Remove, or click a name to read its SKILL.md.
- New session: type `/` in the composer; installed skills are listed with their repository as the source.
- Feature off: the Skills entry is absent from Settings and the routes answer 404.

## Driving it with the repro environment

Preconditions: start the isolated instance with `OMNIGENT_FEATURES=skills_marketplace`. Fetching needs `git` on the server and network access to GitHub; the automated tests replace the clone with a copy of a fixture repository, so they need neither.

- Sources, catalog, install, content, enable, update and remove through the API: `tests/server/routes/test_skill_marketplace.py::test_sources_catalog_and_install_round_trip`; URL preview and rejected sources: `tests/server/routes/test_skill_marketplace.py::test_preview_from_url_and_bad_sources`; feature off: `tests/server/routes/test_skill_marketplace.py::test_routes_are_absent_when_the_feature_is_off`.
- Installed skills reaching a session's bundle, with scope, enablement and name collisions: `tests/server/routes/test_skill_marketplace.py::test_served_bundle_carries_the_owners_enabled_skills`; the spec filter: `tests/server/routes/test_skill_marketplace.py::test_skill_allowed_honours_the_spec_filter`.
- Repository layouts and precedence: `tests/skills/test_catalog.py::test_plain_repo_lists_root_and_namespaced_skills` and `tests/skills/test_catalog.py::test_marketplace_indexes_in_repo_plugins_and_reports_the_rest`; the hardened fetch: `tests/skills/test_git_source.py::test_clone_is_hardened`.
- The Settings page, installed controls, browsing, URL preview and the admin gate: `pnpm --filter web exec vitest run src/pages/settings/SettingsSkillsSection.test.tsx`.
- Human check end to end: with the feature on, add a real GitHub repository of skills as a source, install one, start a new session on a host and in a sandbox, type `/` and confirm the skill is listed with its repository, then invoke it.

## Gotchas

- Only GitHub repositories are accepted; a GitLab or arbitrary git URL is rejected with a message, not cloned.
- A ref must be a branch or tag. A commit SHA cannot be shallow-cloned by name and fails with a hint.
- A running session keeps the skills it started with; installs show up on the next session start.
- Catalogs are cached for ten minutes per source and ref; use Refresh (`?refresh=true`) after pushing to the repository.
- Marketplace plugins whose source is another repository are reported under "skipped", not followed.
