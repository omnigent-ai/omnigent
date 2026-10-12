# Azure DevOps pull requests

Azure DevOps Services sessions show pull requests in the shared Pull Requests
panel, composer controls, and Canvas cards. The execution host's PAT or Azure
CLI sign-in reads the request. The panel identifies Azure requests with `!`.

## Sub-features

- `summary`: organization/project/repository, request title, description and status.
- `changes`: local file diff and expanded context pinned to displayed revisions.
- `associations`: link, select and unlink a request from another repository.
- `partial`: retain loaded results and explain incomplete data or unavailable diffs.
- `auth`: an authenticated PAT works without the Azure CLI; account switching is absent.
- `tracking`: supported successful agent mutations associate their resulting request.

## How to get to it (user POV)

- `desktop-rail`: open Workspace and choose Pull Requests.
- `desktop-composer`: click the request number beside the session composer.
- `mobile-composer`: tap the request number to open the full-screen panel.
- `canvas`: when Canvas is enabled, follow a request link on a session card.
- `agent-tool`: create or update a request through the session's agent tools.

## Driving it with the repro environment

Preconditions: use the [Verify Omnigent skill](skills/verify-omnigent/SKILL.md)
and an isolated instance. Browser fixtures replace provider responses and do
not contact Azure DevOps or invoke the Azure CLI.

- `desktop-rail`, `desktop-composer`, and `mobile-composer`:
  `tests/e2e_ui/azure_devops/test_azure_devops_tab.py::test_azure_devops_tab_shows_provider_repo_and_pull_request`
  opens Summary and Changes, then expands unchanged context. The rail variant
  also proves a signed-in credential works without the Azure CLI.
- Associations:
  `tests/e2e_ui/azure_devops/test_azure_devops_tab.py::test_azure_link_select_unlink_explains_outside_workspace_diff`
  links another repository's request, shows its diff limitation, switches
  selection and removes it.
- Partial and unsupported states:
  `tests/e2e_ui/azure_devops/test_azure_devops_tab.py::test_azure_partial_results_keep_loaded_data_and_explain_unavailable_diff`
  and
  `tests/e2e_ui/azure_devops/test_azure_devops_tab.py::test_unsupported_remote_shows_empty_state_without_a_provider_name`
  check visible explanations without implying an empty successful response.
- `canvas`:
  `tests/e2e_ui/azure_devops/test_azure_devops_tab.py::test_azure_canvas_link_uses_provider_identity`
  checks the request's `!` number and exact external URL.
- `agent-tool`: browser fixtures do not verify automatic tracking. Use a permitted
  test repository and the [observer instructions](../docs/AZURE_DEVOPS.md#observer-limits);
  after a successful supported creation or update, confirm the resulting request
  appears in the panel. Reads and comments must not add an association.

## Gotchas

- The tab retains the generic Pull Requests label; its provider heading, number
  and Canvas link use Azure DevOps metadata.
- An authenticated PAT does not require the Azure CLI. Managed sandbox credential
  provisioning and Azure DevOps Server on premises are separate features.
- Linked requests outside the checkout can show a summary but have no local
  diff. Missing commits may finish fetching in the background; refresh to retry.
- Mocked browser evidence proves rendering and interaction, not live account
  access, local git fetching, or native agent tracking.
- Keep screenshots and recordings outside the tracked tree.
