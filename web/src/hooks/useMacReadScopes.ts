// Keeps the page's QueryClient in step with Mac read scopes: a scope whose
// identity changed drops its queries. The binding lasts as long as the client,
// so a change while no reader is mounted still drops what it cached. What this
// device kept for a scope follows the same changes from then on.

import { type QueryClient, useQueryClient } from "@tanstack/react-query";

import { bindMacReadScopes } from "@/lib/macReadScopeQueries";
import { watchPullRequestDetailsCache } from "@/lib/pullRequestDetailsCache";
import { bindPullRequestSummaryDefaults } from "@/lib/pullRequestRecord";
import { watchRepositoryLabelCache } from "@/lib/repositoryLabelCache";

const bound = new WeakSet<QueryClient>();

/** Bind the current QueryClient to Mac read scopes, once for its lifetime. */
export function useMacReadScopes(): void {
  const client = useQueryClient();
  // Bound during render, so no read keyed under a scope can run before it.
  if (!bound.has(client)) {
    bound.add(client);
    bindMacReadScopes(client);
    bindPullRequestSummaryDefaults(client);
    watchRepositoryLabelCache();
    watchPullRequestDetailsCache();
  }
}
