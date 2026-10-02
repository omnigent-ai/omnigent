# GitLab integration delivery plan

Ship reusable git providers first, then GitLab's sidepanel and both kinds of
hooks, then managed connections and credentials, and finally supported
self-hosted deployments. The first customer milestone ends after phase 2;
it must work with credentials already configured on the execution host.

Related issues: [GitLab integration](https://github.com/omnigent-ai/omnigent/issues/7542)
and [generic git providers](https://github.com/omnigent-ai/omnigent/issues/8456).

## Delivery stack

| Phase | Reviewable change                                                                                                            | Dependency   | Done when                                                                                                                                                     |
| ----- | ---------------------------------------------------------------------------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | Generic provider foundation, continuing [#8458](https://github.com/omnigent-ai/omnigent/pull/8458)                           | Current main | GitHub behavior is preserved; Azure DevOps and external-provider contract tests pass; shared panel and tool observer dispatch through provider adapters.      |
| 2a    | GitLab.com sidepanel and agent tool hooks                                                                                    | Phase 1      | Existing host `glab` authentication enables MR details, checks, comments, diffs, manual MR linking/unlinking, and automatic tracking of successful MR writes. |
| 2b    | Incoming GitLab.com webhooks                                                                                                 | Phase 2a     | An authenticated, configured event launches the selected agent with the correct repository/MR context; retries cannot create duplicate runs.                  |
| 3a    | Generic connections, credentials, and policy adapters, continuing [#8582](https://github.com/omnigent-ai/omnigent/pull/8582) | Phase 2      | Existing GitHub connect, repository selection, credential brokering, and policy behavior pass regression checks through the common interfaces.                |
| 3b    | GitLab.com OAuth and managed sandbox integration                                                                             | Phase 3a     | Connect/disconnect, token refresh, repository/branch selection, initial clone, and subsequent Git/`glab` authentication work under the user's identity.       |
| 4     | Supported self-hosted GitLab deployments                                                                                     | Phase 3b     | The same sidepanel, both hook paths, OAuth, and sandbox journeys pass against a configured private GitLab instance.                                           |

Phases 2a and 2b are separate PRs so the sidepanel can land while webhook
delivery and triggering are reviewed. The urgent milestone includes both.
OAuth and managed-sandbox provisioning are not prerequisites for this milestone.

## First action: finish the existing foundation

Carry #8458 intact, including its Azure DevOps adapter, preserving Tyler Lynch's
commits. Azure exercises the abstraction with a real second provider; removing
it would require splitting existing code, fixtures, and documentation.

Use `dhruvgupta/omni-10545-git-providers` for the foundation, then stack
`dhruvgupta/omni-10545-gitlab-sidepanel` and
`dhruvgupta/omni-10545-gitlab-webhooks` on it. Add focused compatibility fixes
as our own commits. Link replacement PRs to their source PRs and tracking issues;
do not close the original PRs merely because a replacement exists.

The takeover fixes the missing Azure DevOps entry in the feature-map index
and makes the server-selector test wait for its button to become enabled. The
selector CI failure did not reproduce on the baseline or the merged branch.
On the branch merged with current main, 787 backend regression tests and 189
focused frontend tests passed; browser and live-provider checks are separate
verification steps. Obtain maintainer review after required checks pass.

## Phase 2a: sidepanel and agent tool hooks

Add a GitLab descriptor, API/client adapter, pull-request adapter, and observer
adapter. Reuse `pr_resource`, `pr_observer`, session associations,
`usePullRequests`, and `PullRequestPanel`. Add GitLab branding and terminology
to the shared frontend provider registry.

Adapt the useful GitLab URL, CLI, and API work from
[#8116](https://github.com/omnigent-ai/omnigent/pull/8116), retaining contributor
credit. Do not carry its separate GitLab panel, routes, or OAuth availability
gate into this slice. Existing host `glab` authentication must be sufficient.

Normalize GitLab MR IIDs, states, pipelines/jobs, notes, changed files, and
before/after file contents into the common contract. Preserve full nested
project paths and source-project identity for fork diffs. Paginated or
truncated responses must report incomplete data instead of invented counts.

Agent hooks recognize successful `glab mr` and supported GitLab MCP writes.
GitLab has its own command verbs, including `update`, `approve`, `revoke`,
`merge`, `close`, and `reopen`; translating them into GitHub commands is not
correct. Reads, comments alone, failed commands, and links quoted in content
must not create MR associations. Observer hooks must not make network calls.

Acceptance includes current-branch discovery, manual attachment/removal,
checks/comments/diffs, renamed files and expanded context, nested groups,
mixed-provider references, and durable tracking after successful CLI/MCP writes.
Verify desktop rail, narrow-screen drawer, composer links, and canvas cards.

## Phase 2b: incoming webhooks

Incoming GitLab webhooks are separate from agent tool hooks. The existing
GitHub connection does not consume external webhooks, and scheduled tasks
currently require a recurrence rule; neither is an existing generic webhook
execution service.

Implement the smallest concrete event path: a configured project-to-agent
binding, an authenticated receiver, a GitLab event adapter, a durable delivery
record, and dispatch into the existing session/host launch machinery. Add a
shared event contract at this point, rather than requiring a general event
framework in phase 1.

Configure the executing owner, agent, host/workspace, allowed project, and
enabled events explicitly. Verify the configured webhook authentication before
processing the body. Use a retry-stable delivery identifier scoped to the
subscription, record acceptance durably before acknowledging it, and distinguish
retryable dispatch errors from completed deliveries. Persist the delivery-to-session
identity so a crash after session creation resumes the same run on retry.
Do not trust project or
instance URLs in an unauthenticated payload or forward credentials to them.

The initial trigger policy needs a product decision: MR open/update events,
explicit commands in MR comments, or both configurable per project. Keep
automatic execution opt-in and prevent bot-generated events from looping.
Automated tests must cover rejected authentication, unconfigured projects,
irrelevant events, retries across workers/restarts, and one accepted event
launching exactly one correctly scoped run.

## Phases 3 and 4: managed integration and self-hosting

Port the connection, encrypted credential storage, refresh, and policy work
from #8116 onto #8582's common interfaces. Complete initial sandbox cloning:
#8582 explicitly retains a GitHub-only initial-clone path, so merging it alone
does not deliver GitLab provisioning.

Keep one provider ID, `gitlab`, across instances. From phase 2 onward, pass the
instance host and complete project identity explicitly through API/auth calls
and include the instance in token/cache keys. `gitlab.com` is the initial
supported default, not an assumption embedded in shared panel or hook logic.

Phase 4 adds and validates instance configuration, OAuth/API origins, host-scoped
credentials, instance-specific login guidance, webhook reachability, and trusted
certificate configuration. Decide support for non-default ports and URL prefixes
explicitly; current generic host parsing does not represent both. Test multiple
instances with identical project paths and MR IIDs to prevent collisions or
credential crossover.

Self-hosted customer acceptance remains a phase-4 gate. A cloud-only phase-2
demo does not establish compatibility with a private GitLab deployment.

## Contribution and verification

Preserve original authorship and sign-offs when carrying commits. Credit
Aleksandr Nekrasov when adapting substantial code from #8116, and sign off our
own commits. Keep all source PRs linked in the new PR descriptions. Each PR
must use the repository template and have its own narrowly stated acceptance
criteria; only the final completing change should close the overall issue.

For the foundation, run provider/parser/observer/resource tests, shared panel
tests, the feature-map suite, and existing GitHub/Azure UI journeys. Follow the
feature-map verification skill for isolated UI runs and keep evidence outside
the tracked tree. For GitLab, exercise both a real authenticated checkout and
a test webhook delivery before claiming the customer milestone complete.
