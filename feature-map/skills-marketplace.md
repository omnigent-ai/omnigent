# Skills marketplace

Settings → Skills lets a user browse repositories of skills, install the ones they want, and have them offered in every new session's slash menu on any host or sandbox. Administrators manage which repositories are browsable; anyone can preview a GitHub repository by URL. Off by default, behind the `skills_marketplace` feature.

## Sub-features

- `skill-sources`: administrators add a GitHub repository (`owner/repo` or URL, optional branch or tag) to the browse list and remove it again; removing a source leaves installed skills in place.
- `skill-catalog`: a source's skills, with name, description, plugin tag for marketplace plugins, path and commit, searchable; skills the user already installed show Installed instead of a button; entries the marketplace could not read are listed as skipped.
- `skill-preview-url`: previewing any GitHub repository by URL shows its catalog without adding it as a source, and installs from it the same way.
- `skill-install-scope`: a user installs for themselves; an administrator can install for everyone.
- `installed-skills`: the Installed tab lists each skill with its source, ref, commit and scope, with enable/disable, Update (refetch at the same ref), Remove behind a confirmation, and a SKILL.md preview.
- `skills-in-session`: an enabled installed skill appears in the new-session slash menu and is usable in the session, on a connected host and in a managed sandbox, for every harness; the agent spec's `skills:` filter still applies and a bundled skill of the same name wins.
- `skill-policy`: the server config's `skills_marketplace:` block binds admins and the local user alike: `sources` are seeded at startup and shown with a Config tag nobody can remove, `allowed_sources` globs refuse other repositories on every fetch path and stop serving skills already installed from them, `allow_url_install: false` hides Add from URL and refuses installs from unlisted repositories, and `credentials` send an operator token to matching private repositories only.
- `skill-enterprise-hosts`: a URL on a GitHub Enterprise Server host the server knows (`GH_HOST`, `OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS`, gh's signed-in hosts) is accepted with its host kept in the clone URL and label; an unknown host is refused rather than rewritten to github.com.

## How to get to it (user POV)

- Settings → Skills → Browse: pick a source on the left, search, Install on a card; or paste a repository under Add from URL and Preview skills.
- Settings → Skills → Installed: toggle, Update, Remove, or click a name to read its SKILL.md.
- New session: type `/` in the composer; installed skills are listed with their repository as the source.
- Feature off: the Skills entry is absent from Settings and the routes answer 404.

## Driving it with the repro environment

Preconditions: start the isolated instance with `OMNIGENT_FEATURES=skills_marketplace`. Fetching needs `git` on the server and network access to GitHub; the automated tests replace the clone with a copy of a fixture repository, so they need neither.

- Sources, catalog, install, content, enable, update and remove through the API: `tests/server/routes/test_skill_marketplace.py::test_sources_catalog_and_install_round_trip`; URL preview and rejected sources: `tests/server/routes/test_skill_marketplace.py::test_preview_from_url_and_bad_sources`; feature off: `tests/server/routes/test_skill_marketplace.py::test_routes_are_absent_when_the_feature_is_off`.
- Installed skills reaching a session's bundle: through the HTTP agent-contents route a runner calls, `tests/server/routes/test_skill_marketplace.py::test_served_bundle_route_carries_installed_skills`; the injection rules (scope, enablement, namespaced and canonical name collisions, a corrupt tarball) at the helper, `tests/server/routes/test_skill_marketplace.py::test_served_bundle_carries_the_owners_enabled_skills`; user-over-org precedence, `tests/server/routes/test_skill_marketplace.py::test_a_users_install_wins_over_the_orgs_whatever_the_order`; the spec filter: `tests/server/routes/test_skill_marketplace.py::test_skill_allowed_honours_the_spec_filter`.
- The feature switched off after installs: `tests/server/routes/test_skill_marketplace.py::test_feature_off_hides_routes_discovery_and_delivery_for_existing_installs`; authorization (admin-only sources and org installs, cross-user visibility and mutation): `tests/server/routes/test_skill_marketplace.py::test_authorization_boundaries`; pagination: `tests/server/routes/test_skill_marketplace.py::test_lists_paginate_with_a_continuation_cursor` and `tests/stores/test_skill_store.py::test_list_pages_continue_without_omissions_or_duplicates`.
- Repository layouts and precedence: `tests/skills/test_catalog.py::test_plain_repo_lists_root_and_namespaced_skills` and `tests/skills/test_catalog.py::test_marketplace_indexes_in_repo_plugins_and_reports_the_rest`; links never followed and oversized metadata skipped: `tests/skills/test_catalog.py::test_links_are_never_followed` and `tests/skills/test_catalog.py::test_oversized_or_multibyte_frontmatter_is_skipped`; the hardened, credential-isolated fetch: `tests/skills/test_git_source.py::test_clone_is_hardened` and `tests/skills/test_git_source.py::test_isolated_clone_drops_inherited_git_config_and_helpers`; packaging limits: `tests/skills/test_git_source.py::test_tar_untrusted_dir_stops_at_the_limits_before_reading_more`.
- The operator policy end to end (seeded sources, refused repositories, URL installs off, delivery stopped by a tightened policy): `tests/server/routes/test_skill_marketplace.py::test_server_policy_binds_sources_url_installs_and_delivery`; parsing, globs and scoped tokens: `tests/skills/test_policy.py`; the fetcher refusing before a clone and passing the token: `tests/skills/test_service.py::test_fetcher_refuses_a_source_outside_the_policy_before_cloning` and `tests/skills/test_service.py::test_fetcher_hands_the_policys_token_to_the_clone`; the token travelling in the environment: `tests/skills/test_git_source.py::test_token_travels_in_the_environment_not_the_command_line`; Enterprise hosts: `tests/skills/test_git_source.py::test_enterprise_hosts_the_server_knows_keep_their_host`.
- The Settings page, installed controls, browsing, URL preview, the admin gate and the config-source and URL-install policy: `pnpm --filter web exec vitest run src/pages/settings/SettingsSkillsSection.test.tsx`.
- Human check end to end: with the feature on, add a real GitHub repository of skills as a source, install one, start a new session on a host and in a sandbox, type `/` and confirm the skill is listed with its repository, then invoke it.

## Gotchas

- Only GitHub repositories are accepted; a GitLab or arbitrary git URL is rejected with a message, not cloned. A GitHub Enterprise host must be one the server knows, or its URL is refused.
- The `skills_marketplace:` config block overrides admin rights: a repository outside `allowed_sources` is refused even for an admin, and skills already installed from it stop being served once the policy tightens.
- A credential token is only ever sent to a repository that matches both `allowed_sources` and its `match`; an unset `token_env` is logged and the clone proceeds anonymously.
- A ref must be a branch or tag. A commit SHA cannot be shallow-cloned by name and fails with a hint.
- A running session keeps the skills it started with; installs show up on the next session start.
- Lists are paged (50 per page by default, 200 at most); Settings shows Load more when a page follows.
- When a user and the org both install the same name, the user's copy wins for that user.
- Turning the feature off keeps installed rows but stops every reader: the routes, discovery and served bundles.
- Catalogs are cached for ten minutes per source and ref; use Refresh (`?refresh=true`) after pushing to the repository.
- Marketplace plugins whose source is another repository are reported under "skipped", not followed.
