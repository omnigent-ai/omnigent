import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useMemo, useState } from "react";
import { AlertCircleIcon } from "lucide-react";

import { QuickPickDialog, type QuickPickItem } from "@/components/ui/quick-pick";
import { Spinner } from "@/components/ui/spinner";
import { showToast } from "@/components/ui/toast";
import {
  useAttachSessionPullRequest,
  useCanKeepLinksOnThisMac,
  useSessionHostOffline,
} from "@/hooks/useAttachSessionPullRequest";
import type { Conversation } from "@/hooks/useConversations";
import { contextRepositories } from "@/lib/branchPullRequest";
import { usePullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  storesSessionPullRequests,
  useSessionPullRequests,
} from "@/newPullRequestPage/hooks/useSessionPullRequests";
import { usePullRequestPreview } from "@/hooks/usePullRequestPreview";
import { usePullRequestSuggestions } from "@/hooks/usePullRequestSuggestions";
import { useWorkspaceAsleep } from "@/hooks/useWorkspaceChangedFiles";
import { useSandboxResume } from "@/hooks/useSandboxResume";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import {
  contextHostProvider,
  readsOnThisMac,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import { pullRequestFastTrackAccount } from "@/lib/githubFastTrack";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { gitProviderCopy } from "@/lib/gitProviders";
import { useCodeHostContext } from "@/lib/codeHostContext";
import { attachPullRequest, usePullRequestAttachStore } from "@/store/pullRequestAttach";
import { useSessionPullRequestIntentStore } from "@/store/sessionPullRequestIntents";
import { pullRequestKey } from "@/lib/pullRequestReference";
import {
  MAX_SESSION_PULL_REQUESTS,
  SESSION_PULL_REQUEST_LIMIT_REASON,
  sessionPullRequestMembership,
} from "@/lib/sessionPullRequestMembership";
import { showLinkedPullRequest } from "./pullRequestLinks/showLinkedPullRequest";

import {
  type AttachPullRequestContext,
  attachPullRequestFromInput,
  attachPullRequestHint,
  attachPullRequestSuggestions,
  attachPullRequestWaitsForRemote,
  isGitSpokePullRequestInput,
  pullRequestShortLabel,
  workspaceRemoteEvidence,
} from "./attachPullRequestItems";
import { linkNeedsHostReason } from "./linkPullRequestAvailability";

function withIcon<Result>(result: Result, icon: ReactNode): Result {
  return result && typeof result === "object" && "id" in result ? { ...result, icon } : result;
}

/**
 * The shared "Link a pull request" picker. Mounted once; the PR panel and ⌘K
 * open it for a session through the attach store.
 */
export function AttachPullRequestDialog({
  conversations,
}: {
  conversations?: readonly Conversation[];
}) {
  const openSessionId = usePullRequestAttachStore((state) => state.openSessionId);
  // Keep the last session mounted so the dialog can animate closed.
  const [sessionId, setSessionId] = useState<string | null>(openSessionId);
  if (openSessionId && openSessionId !== sessionId) setSessionId(openSessionId);
  if (!sessionId) return null;
  return (
    <AttachPullRequestPicker
      key={sessionId}
      sessionId={sessionId}
      open={openSessionId === sessionId}
      conversations={conversations}
    />
  );
}

function AttachPullRequestPicker({
  sessionId,
  open,
  conversations,
}: {
  sessionId: string;
  open: boolean;
  conversations?: readonly Conversation[];
}) {
  const close = usePullRequestAttachStore((state) => state.close);
  const draft = usePullRequestAttachStore((state) => state.drafts[sessionId] ?? "");
  const setDraft = usePullRequestAttachStore((state) => state.setDraft);
  const attempt = usePullRequestAttachStore((state) => state.attempts[sessionId]);

  const info = usePullRequestInfo(sessionId, { poll: false });
  // The checkout's remotes, read on this Mac, resolve a typed number without the host.
  const stored = useSessionPullRequests(sessionId, info.data);
  const storesPrs = storesSessionPullRequests(useServerInfo());
  const hostOffline = useSessionHostOffline(sessionId, info);
  const sandboxAsleep = useWorkspaceAsleep(sessionId);
  const sandboxResuming = useSandboxResume(sessionId).status === "resuming";
  const attach = useAttachSessionPullRequest(sessionId);
  const queryClient = useQueryClient();
  const linksOnThisMac = useCanKeepLinksOnThisMac();

  const storedPrs = stored.data?.prs;
  const livePrs = info.data?.prs;
  // No payload yet means no provider is known; a stored PR can name it.
  const provider = info.data ? info.data.provider : (storedPrs?.[0]?.provider ?? null);
  const copy = gitProviderCopy(
    provider,
    undefined,
    info.data?.provider_display ?? storedPrs?.[0]?.provider_display,
  );

  // The selected account's host is authoritative; otherwise every signed-in host.
  const accounts = info.data?.auth?.accounts ?? info.data?.accounts;
  const selectedLogin = info.data?.auth?.selected_account ?? info.data?.selected_account;
  const selectedHost = accounts?.find((account) => account.login === selectedLogin)?.host;
  const repository = info.data?.repo?.name_with_owner ?? storedPrs?.[0]?.repository ?? null;
  // The workspace's own remote names the repository's host where the host's payload doesn't.
  const codeHost = useCodeHostContext(sessionId, { enabled: open });
  const workspaceRemote = useMemo(
    () =>
      workspaceRemoteEvidence({ data: codeHost.data, isFetching: codeHost.isFetching }, repository),
    [codeHost.data, codeHost.isFetching, repository],
  );

  // Hosts that can't link PRs, or that are offline, explain why instead of failing on submit.
  const unavailableReason =
    storesPrs && hostOffline
      ? linkNeedsHostReason(sandboxAsleep, sandboxResuming)
      : info.data?.tracking_available === false
        ? `This session’s host can’t link ${copy.requestName}s. Update the host to link one.`
        : null;

  // GitHub suggests PRs as the host's account; elsewhere the picker takes a pasted link.
  const fastTrackAccount = useMemo(
    () =>
      pullRequestFastTrackAccount({
        provider,
        prHost: selectedHost,
        selectedAccount: selectedLogin,
      }),
    [provider, selectedHost, selectedLogin],
  );
  // This Mac suggests PRs as its own identity where it reads the session's code host.
  const suggestionsHost = suggestionHost(provider ?? null, selectedHost, [
    ...(livePrs ?? []),
    ...(storedPrs ?? []),
  ]);
  const macSuggestions = useMemo(
    () =>
      (provider === "github" || provider === "gitspoke") && suggestionsHost
        ? { provider: provider as "github" | "gitspoke", host: suggestionsHost }
        : null,
    [provider, suggestionsHost],
  );
  const suggestions = usePullRequestSuggestions(
    fastTrackAccount,
    repository,
    open && unavailableReason === null,
    macSuggestions,
  );
  const loadedSuggestions = suggestions.status === "ready" ? suggestions.suggestions : null;
  // A typed link this Mac reads is looked up here, so the item shows the PR before linking.
  const preview = usePullRequestPreview(draft, open, sessionId);
  const previewed = preview.status === "ready" ? preview.preview : null;

  const intents = useSessionPullRequestIntentStore((store) => store.bySession[sessionId]);
  // The PR being linked from here says "Linking…", not that it's already linked.
  const linkingKey =
    attempt?.status === "pending" && attempt.url ? pullRequestKey(attempt.url) : null;
  const linkedKeys = useMemo(() => {
    const { [linkingKey ?? ""]: _linking, ...others } = intents ?? {};
    return sessionPullRequestMembership({ stored: stored.data, live: livePrs, intents: others })
      .keys;
  }, [stored.data, livePrs, intents, linkingKey]);
  const readPathVersion = useDesktopReadPathVersion();
  const context = useMemo<AttachPullRequestContext>(() => {
    const sessionPullRequests = [...(livePrs ?? []), ...(storedPrs ?? [])];
    return {
      sessionId,
      provider,
      repository,
      numberPrefix: copy.prNumberPrefix,
      requestName: copy.requestName,
      // The same membership every surface uses; a PR inferred from the branch isn't in it.
      linkedUrls: [...linkedKeys],
      full: linkedKeys.size >= MAX_SESSION_PULL_REQUESTS,
      sessionPullRequests,
      accountHosts: selectedHost
        ? [selectedHost]
        : (accounts ?? []).flatMap((account) => (account.host ? [account.host] : [])),
      workspaceRemote,
      contextRepositories: contextRepositories(codeHost.data, contextHostProvider),
      branchPullRequest: info.data?.pr ?? null,
      conversations,
      suggestions: loadedSuggestions,
      preview: previewed,
    };
    // `contextHostProvider` reads the read-path store; recompute once it learns a host.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    sessionId,
    provider,
    repository,
    info.data,
    accounts,
    selectedHost,
    workspaceRemote,
    livePrs,
    storedPrs,
    linkedKeys,
    copy,
    conversations,
    loadedSuggestions,
    codeHost.data,
    previewed,
    readPathVersion,
  ]);

  const ProviderIcon = copy.Icon;
  const icon = useMemo(() => <ProviderIcon className="size-4" aria-hidden />, [ProviderIcon]);
  // A typed number waiting on the workspace's remotes offers nothing, not even a fuzzy match.
  const waitingForRemote = attachPullRequestWaitsForRemote(draft, context);
  const items = useMemo(
    () =>
      unavailableReason || waitingForRemote
        ? []
        : attachPullRequestSuggestions(context).map((item) => ({ ...item, icon })),
    [context, icon, unavailableReason, waitingForRemote],
  );

  // The attempt lives in the store, so it survives the picker closing or
  // switching sessions while the host answers.
  const handleAccept = (item: QuickPickItem<string>) => {
    const typed = usePullRequestAttachStore.getState().drafts[sessionId] ?? "";
    if (attachPullRequestWaitsForRemote(typed, context)) return "keepOpen" as const;
    void attachPullRequest(sessionId, item.value, attach.mutateAsync).then((linkedUrl) => {
      if (!linkedUrl) return;
      showToast(`Linked ${pullRequestShortLabel(linkedUrl, copy.prNumberPrefix)}`);
      // The tabs sidebar shows it in its own tab; Classic's panel already selects it.
      showLinkedPullRequest(queryClient, sessionId, linkedUrl, "stay");
    });
    return "keepOpen" as const;
  };

  // Linking runs through the host, so show progress and failures in the footer.
  const attachStatus =
    attempt?.status === "pending" ? (
      <span className="flex items-center gap-1.5">
        <Spinner aria-hidden role="presentation" className="size-3.5" />
        Linking…
      </span>
    ) : attempt?.status === "failed" ? (
      <span role="alert" className="flex items-center gap-1.5 text-destructive">
        <AlertCircleIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="truncate" title={linkFailureMessage(attempt)}>
          {linkFailureMessage(attempt)}
        </span>
      </span>
    ) : context.full ? (
      <span role="status" className="flex items-center gap-1.5 text-muted-foreground">
        <AlertCircleIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="truncate" title={SESSION_PULL_REQUEST_LIMIT_REASON}>
          {SESSION_PULL_REQUEST_LIMIT_REASON}
        </span>
      </span>
    ) : waitingForRemote ? (
      <span className="flex items-center gap-1.5">
        <Spinner aria-hidden role="presentation" className="size-3.5" />
        Checking this workspace’s git remote…
      </span>
    ) : preview.status === "loading" ? (
      <span className="flex items-center gap-1.5">
        <Spinner aria-hidden role="presentation" className="size-3.5" />
        Checking the {copy.requestName} on this Mac…
      </span>
    ) : preview.status === "error" ? (
      <span className="flex items-center gap-1.5 text-muted-foreground">
        <AlertCircleIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="truncate" title={preview.message}>
          This Mac couldn’t check it. {preview.message}
        </span>
      </span>
    ) : suggestions.status === "loading" ? (
      <span className="flex items-center gap-1.5">
        <Spinner aria-hidden role="presentation" className="size-3.5" />
        Finding {copy.requestName}s on GitHub…
      </span>
    ) : suggestions.status === "error" ? (
      <span className="flex items-center gap-1.5 text-muted-foreground">
        <AlertCircleIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="truncate">Couldn’t load suggestions. {suggestions.message}</span>
      </span>
    ) : null;

  // GitHub links have one shape, so show it; other providers' links vary.
  const linkHint = attachPullRequestHint(context, copy.defaultHost ? copy.prUrlPlaceholder : null);
  const placeholder = `Paste a ${copy.requestName} link or type ${copy.prNumberPrefix}123`;
  const emptyMessage =
    unavailableReason ??
    (waitingForRemote
      ? repository
        ? `Checking which server hosts ${repository} before linking ${draft.trim()}…`
        : `Checking this workspace’s repositories before linking ${draft.trim()}…`
      : draft.trim()
        ? `No matching ${copy.requestName}s. ${linkHint}`
        : `Paste a ${copy.requestName} link, or type ${copy.prNumberPrefix}123 for one in this repository.`);
  return (
    <QuickPickDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close();
      }}
      label={`Link a ${copy.requestName}`}
      placeholder={placeholder}
      segments={[{ sourceId: "items", ranking: "client", items }]}
      query={draft}
      onQueryChange={(next) => setDraft(sessionId, next)}
      createItem={(input) =>
        // A GitSpoke link is kept on this Mac, so it doesn't need the host.
        unavailableReason && !(linksOnThisMac && isGitSpokePullRequestInput(input))
          ? { error: unavailableReason }
          : withIcon(attachPullRequestFromInput(input, context), icon)
      }
      emptyMessage={emptyMessage}
      acceptLabel="Link"
      onAccept={handleAccept}
      summary={() => attachStatus}
    />
  );
}

/** The host the session's PRs live on, to suggest more from: the account's, else the PRs'. */
function suggestionHost(
  provider: string | null,
  accountHost: string | null | undefined,
  pullRequests: readonly { url: string }[],
): string | null {
  if (accountHost) return accountHost.toLowerCase();
  for (const pr of pullRequests) {
    try {
      return new URL(pr.url).hostname.toLowerCase();
    } catch {
      // Not a link; try the next one.
    }
  }
  return provider === "github" ? "github.com" : null;
}

/**
 * Why linking failed. A GitHub PR this Mac reads still links through the session's
 * machine, which has to confirm it, so say that rather than leave it unexplained.
 */
function linkFailureMessage(attempt: { draft: string; message: string }): string {
  const target = parseCodeHostPullRequestUrl(attempt.draft.trim());
  if (target?.provider !== "github" || !readsOnThisMac(target.url)) return attempt.message;
  return `${attempt.message} This Mac can read the pull request, but linking a GitHub pull request still needs the session’s machine to confirm it.`;
}
