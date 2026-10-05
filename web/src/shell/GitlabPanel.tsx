import {
  AlertCircleIcon,
  ExternalLinkIcon,
  GitBranchIcon,
  GitPullRequestIcon,
  KeyRoundIcon,
  LinkIcon,
  Loader2Icon,
  TerminalIcon,
  UnlinkIcon,
} from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { GitlabIcon } from "@/components/icons/GitlabIcon";
import { Input } from "@/components/ui/input";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import {
  type GitlabInfo,
  type GitlabMergeRequest,
  useGitlabInfo,
  useGitlabMrDiff,
  useUpdateSessionGitlabMr,
} from "@/hooks/useGitlab";

function PanelMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-ui text-muted-foreground">
      {children}
    </div>
  );
}

function mergeRequestNumber(mr: GitlabMergeRequest): string | null {
  const number = mr.iid ?? mr.id;
  return typeof number === "number" ? `!${number}` : null;
}

export function relatedGitlabMergeRequests(
  mergeRequests: GitlabMergeRequest[] | undefined,
  selectedUrl: string | null | undefined,
): GitlabMergeRequest[] {
  return (mergeRequests ?? []).filter(
    (candidate) => candidate.web_url && candidate.web_url !== selectedUrl,
  );
}

export type GitlabPanelState =
  | { kind: "loading" }
  | { kind: "runner-offline" }
  | { kind: "error"; message: string }
  | { kind: "not-a-git-repo" }
  | { kind: "no-glab-cli" }
  | { kind: "repo-unresolved" }
  | { kind: "no-mr"; branch?: string }
  | { kind: "unavailable"; message?: string }
  | { kind: "ready" };

export function deriveGitlabPanelState(info: {
  isLoading: boolean;
  error: unknown;
  data: GitlabInfo | undefined;
}): GitlabPanelState {
  if (info.isLoading) return { kind: "loading" };
  if (info.error) {
    if (info.error instanceof RunnerOfflineError) return { kind: "runner-offline" };
    return { kind: "error", message: (info.error as Error).message };
  }
  const data = info.data;
  if (!data || !data.available) {
    if (data?.reason === "not_a_git_repo") return { kind: "not-a-git-repo" };
    return { kind: "unavailable", message: data?.message };
  }
  if (data.glab_available === false) return { kind: "no-glab-cli" };
  if (data.authenticated === false || !data.repo?.path_with_namespace) {
    return { kind: "repo-unresolved" };
  }
  if (!data.merge_request) return { kind: "no-mr", branch: data.branch ?? undefined };
  return { kind: "ready" };
}

/** GitLab merge-request panel for the active session's associated MR. */
export function GitlabPanel({ conversationId }: { conversationId: string }) {
  const [attachUrl, setAttachUrl] = useState("");
  const info = useGitlabInfo(conversationId);
  const update = useUpdateSessionGitlabMr(conversationId);
  const mr = info.data?.merge_request;
  const mergeRequests = info.data?.merge_requests ?? (mr ? [mr] : []);
  const diff = useGitlabMrDiff(
    conversationId,
    info.data?.selected_mr_url ?? undefined,
    Boolean(mr),
  );

  const panelState = deriveGitlabPanelState({
    isLoading: info.isLoading,
    error: info.error,
    data: info.data,
  });

  const attachForm = (
    <form
      className="flex gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        const url = attachUrl.trim();
        if (url) update.mutate({ url, action: "attach" });
      }}
    >
      <Input
        aria-label="GitLab merge request URL"
        value={attachUrl}
        onChange={(event) => setAttachUrl(event.target.value)}
        placeholder={`https://${info.data?.repo?.host ?? "gitlab.com"}/group/project/-/merge_requests/1`}
      />
      <Button type="submit" size="icon" disabled={!attachUrl.trim() || update.isPending}>
        <LinkIcon className="size-4" />
        <span className="sr-only">Link merge request</span>
      </Button>
    </form>
  );

  if (panelState.kind === "loading") {
    return (
      <PanelMessage>
        <Loader2Icon className="size-5 animate-spin" />
        Loading GitLab…
      </PanelMessage>
    );
  }
  if (panelState.kind === "runner-offline") {
    return (
      <PanelMessage>The agent is asleep. Send a message to reconnect its runner.</PanelMessage>
    );
  }
  if (panelState.kind === "error") {
    return <PanelMessage>Couldn’t load GitLab info: {panelState.message}</PanelMessage>;
  }
  if (panelState.kind !== "ready") {
    const content = {
      "not-a-git-repo": {
        icon: GitBranchIcon,
        title: "Not a git repository",
        hint: "This workspace isn’t a git checkout, so there’s no branch or merge request to show.",
      },
      "no-glab-cli": {
        icon: TerminalIcon,
        title: "GitLab CLI not found",
        hint: "Install glab on the host to see this branch’s merge request.",
      },
      "repo-unresolved": {
        icon: KeyRoundIcon,
        title: "Can’t reach the upstream GitLab repo",
        hint: "Confirm the origin remote points to GitLab and run glab auth status on the host.",
      },
      "no-mr": {
        icon: GitPullRequestIcon,
        title: `No open MR for ${panelState.kind === "no-mr" ? (panelState.branch ?? "this branch") : "this branch"}`,
        hint: "Merge requests created in this session appear here. You can also link an existing MR.",
      },
      unavailable: {
        icon: AlertCircleIcon,
        title: "GitLab isn’t available",
        hint:
          panelState.kind === "unavailable" && panelState.message
            ? panelState.message
            : "There’s no GitLab information to show for this session.",
      },
    }[panelState.kind];
    const Icon = content.icon;
    return (
      <div className="flex h-full flex-col gap-4 overflow-y-auto p-5">
        <div className="flex flex-1 flex-col items-center justify-center gap-2 text-center">
          <Icon className="size-8 text-muted-foreground/50" />
          <p className="text-ui font-medium text-foreground">{content.title}</p>
          <p className="max-w-xs text-ui text-muted-foreground">{content.hint}</p>
        </div>
        {panelState.kind === "no-mr" && attachForm}
        {update.isError && (
          <p className="text-ui text-destructive">{(update.error as Error).message}</p>
        )}
      </div>
    );
  }

  const data = info.data;
  if (!data || !mr) return <PanelMessage>There’s no GitLab information to show.</PanelMessage>;

  const url = mr.web_url ?? data.selected_mr_url;
  const number = mergeRequestNumber(mr);
  const relatedMergeRequests = relatedGitlabMergeRequests(data.merge_requests, url);
  return (
    <div className="h-full min-h-0 overflow-y-auto">
      {mergeRequests.length > 1 && (
        <div className="border-b border-border p-3">
          <p className="mb-2 text-ui font-medium text-foreground">Merge requests for this branch</p>
          <div className="flex flex-col gap-1">
            {mergeRequests.map((candidate) => {
              const candidateUrl = candidate.web_url;
              const candidateNumber = mergeRequestNumber(candidate);
              return candidateUrl ? (
                <a
                  key={candidateUrl}
                  href={candidateUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-2 rounded px-2 py-1.5 text-ui text-muted-foreground hover:bg-muted hover:text-foreground"
                >
                  <GitlabIcon className="size-4 shrink-0" />
                  <span className="truncate">{candidate.title ?? "GitLab merge request"}</span>
                  {candidateNumber && (
                    <span className="ml-auto tabular-nums">{candidateNumber}</span>
                  )}
                </a>
              ) : null;
            })}
          </div>
        </div>
      )}
      <div className="border-b border-border p-4">
        <div className="flex items-start gap-2">
          <GitPullRequestIcon className="mt-0.5 size-5 shrink-0 text-orange-600" />
          <div className="min-w-0 flex-1">
            <p className="break-words text-ui font-semibold text-foreground">
              {mr.title ?? "GitLab merge request"}
            </p>
            <p className="mt-1 text-ui text-muted-foreground">
              {number ?? "Merge request"}
              {mr.state ? ` · ${mr.state}` : ""}
              {mr.source_branch && mr.target_branch
                ? ` · ${mr.source_branch} → ${mr.target_branch}`
                : ""}
            </p>
          </div>
          {url && (
            <Button asChild variant="ghost" size="icon-xs" aria-label="Open GitLab merge request">
              <a href={url} target="_blank" rel="noreferrer">
                <ExternalLinkIcon className="size-4" />
              </a>
            </Button>
          )}
        </div>
        {mr.author?.username && (
          <p className="mt-2 text-ui text-muted-foreground">{mr.author.username}</p>
        )}
        {mr.description && (
          <p className="mt-3 whitespace-pre-wrap text-ui text-muted-foreground">{mr.description}</p>
        )}
        {relatedMergeRequests.length > 0 && (
          <div className="mt-3 border-t border-border pt-3">
            <p className="mb-1 text-xs font-medium text-muted-foreground">
              Also open for this branch
            </p>
            <div className="flex flex-col gap-1">
              {relatedMergeRequests.map((candidate) => (
                <a
                  key={candidate.web_url}
                  href={candidate.web_url}
                  target="_blank"
                  rel="noreferrer"
                  className="truncate text-ui text-primary hover:underline"
                >
                  {mergeRequestNumber(candidate) ?? "Merge request"}
                  {candidate.title ? ` · ${candidate.title}` : ""}
                </a>
              ))}
            </div>
          </div>
        )}
        {data.selected_mr_url && (
          <Button
            className="mt-3"
            variant="outline"
            size="sm"
            disabled={update.isPending}
            onClick={() => update.mutate({ url: data.selected_mr_url!, action: "detach" })}
          >
            <UnlinkIcon className="size-4" />
            Unlink
          </Button>
        )}
      </div>
      <div className="p-4 pb-16">
        <p className="mb-2 text-ui font-medium text-foreground">Changes</p>
        {diff.isLoading ? (
          <div className="flex items-center gap-2 text-ui text-muted-foreground">
            <Loader2Icon className="size-4 animate-spin" />
            Loading diff…
          </div>
        ) : diff.data?.patch ? (
          <pre className="overflow-x-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-xs leading-5">
            {diff.data.patch}
          </pre>
        ) : (
          <p className="text-ui text-muted-foreground">
            No diff is available for this merge request.
          </p>
        )}
      </div>
    </div>
  );
}
