/**
 * Project settings → Slack: link the project to one Slack channel, where an
 * `@omni` mention reaches the project's Lead. Members see the link; the owner and
 * product approvers change it.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getProjectSlack,
  linkProjectSlack,
  type ProjectSlackLink,
  type ProjectSlackState,
  setProjectSlackEnabled,
  unlinkProjectSlack,
} from "@/lib/projectSlackApi";

const STATE_TEXT: Record<ProjectSlackState, string> = {
  claimed: "Linked; setting up the channel watcher",
  active: "On: @omni in this channel reaches the Lead",
  disabled: "Paused: the channel is kept for this project but not watched",
  error: "Setup failed",
  needs_reconnect: "Needs the owner to sign in; see below",
};

// States where the watcher isn't running and saving again or retrying sets it up.
const RETRY_STATES: ReadonlySet<ProjectSlackState> = new Set(["claimed", "error", "needs_reconnect"]);

/** The link's saved settings to send again, minus what the server fills in. */
function savedDestination(link: ProjectSlackLink | undefined): Record<string, string> {
  const { relay_url: _relayUrl, ...rest } = link?.destination ?? {};
  return rest;
}

function channelLabel(link: ProjectSlackLink): string {
  return link.channel_name ? `#${link.channel_name}` : (link.channel_id ?? "");
}

export function ProjectSlackSection({
  projectId,
  embedded = false,
}: {
  projectId: string;
  /** Inside a page section that already has its own "Slack" heading. */
  embedded?: boolean;
}) {
  const queryClient = useQueryClient();
  const queryKey = ["project-slack", projectId];
  const link = useQuery({ queryKey, queryFn: () => getProjectSlack(projectId) });
  const [channelLink, setChannelLink] = useState("");
  const [channelName, setChannelName] = useState("");
  const [automationUrl, setAutomationUrl] = useState("");
  const onDone = (next: ProjectSlackLink) => queryClient.setQueryData(queryKey, next);
  const save = useMutation({
    mutationFn: () => {
      const current = link.data;
      const destination = savedDestination(current);
      if (automationUrl.trim()) destination.automation_url = automationUrl.trim();
      const channel = channelLink.trim()
        ? { channel_link: channelLink.trim() }
        : {
            channel_id: current?.channel_id,
            slack_team_id: current?.slack_team_id ?? undefined,
          };
      return linkProjectSlack(projectId, {
        ...channel,
        channel_name: channelName.trim() || current?.channel_name || undefined,
        destination,
      });
    },
    onSuccess: (next) => {
      setChannelLink("");
      setChannelName("");
      setAutomationUrl("");
      onDone(next);
    },
  });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => setProjectSlackEnabled(projectId, enabled),
    onSuccess: onDone,
  });
  const unlink = useMutation({ mutationFn: () => unlinkProjectSlack(projectId), onSuccess: onDone });
  const data = link.data;
  const canManage = data?.can_manage === true;
  const busy = save.isPending || toggle.isPending || unlink.isPending;
  const failure = [save, toggle, unlink].find((mutation) => mutation.isError)?.error as
    | Error
    | undefined;
  const canSave =
    channelLink.trim() !== "" || (data?.linked === true && automationUrl.trim() !== "");
  const savedAutomationUrl = data?.destination?.automation_url;
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (canSave) save.mutate();
  };
  return (
    <section
      className={embedded ? "flex flex-col gap-2" : "flex flex-col gap-2 border-t pt-4"}
      data-testid="project-slack"
    >
      {!embedded && <h3 className="font-medium text-ui">Slack</h3>}
      <p className="text-muted-foreground text-sm">
        Link one Slack channel. Anyone in it can <code className="text-xs">@omni</code> the
        project&apos;s Lead, and replies come back in the thread marked{" "}
        <em>via this project · Lead</em>. Only this setting links a channel; nothing in Slack can.
      </p>
      {link.isError && (
        <p className="text-destructive text-sm" role="alert">
          {(link.error as Error).message}
        </p>
      )}
      {data?.linked === true && (
        <div className="flex flex-col gap-1 text-sm" data-testid="project-slack-link">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate font-medium">{channelLabel(data)}</span>
            {data.state !== undefined && (
              <span className="text-muted-foreground" data-testid="project-slack-state">
                {STATE_TEXT[data.state]}
              </span>
            )}
          </div>
          {data.last_error && (
            <p className="text-destructive" role="alert">
              {data.last_error}
            </p>
          )}
          {(data.overlaps ?? []).length > 0 && (
            <p className="text-amber-600 dark:text-amber-400" data-testid="project-slack-overlap">
              Another automation also watches this channel ({data.overlaps?.join(", ")}). Turn
              it off so messages aren&apos;t answered twice.
            </p>
          )}
          {canManage && (
            <div className="flex gap-2 pt-1">
              <Button
                type="button"
                size="sm"
                variant="outline"
                loading={toggle.isPending}
                disabled={busy}
                data-testid="project-slack-toggle"
                onClick={() =>
                  toggle.mutate(
                    data.state === "disabled" ||
                      (data.state !== undefined && RETRY_STATES.has(data.state)),
                  )
                }
              >
                {data.state === "disabled"
                  ? "Resume"
                  : data.state !== undefined && RETRY_STATES.has(data.state)
                    ? "Retry"
                    : "Pause"}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                loading={unlink.isPending}
                disabled={busy}
                data-testid="project-slack-unlink"
                onClick={() => unlink.mutate()}
              >
                Unlink
              </Button>
            </div>
          )}
        </div>
      )}
      {link.isSuccess && !data?.linked && !canManage && (
        <p className="text-muted-foreground text-sm">Not linked to a Slack channel.</p>
      )}
      {canManage && (
        <form onSubmit={onSubmit} className="flex flex-col gap-2" data-testid="project-slack-form">
          <Input
            value={channelLink}
            onChange={(event) => setChannelLink(event.target.value)}
            placeholder="Channel link from Slack (Copy link), e.g. https://example.slack.com/archives/C0456"
            aria-label="Slack channel link"
            data-testid="project-slack-channel-link"
          />
          <Input
            value={channelName}
            onChange={(event) => setChannelName(event.target.value)}
            placeholder="Channel name (optional)"
            aria-label="Slack channel name"
            data-testid="project-slack-channel-name"
          />
          <Input
            value={automationUrl}
            onChange={(event) => setAutomationUrl(event.target.value)}
            placeholder={
              savedAutomationUrl
                ? `Automation API URL: ${savedAutomationUrl}`
                : "Automation API URL, e.g. https://<workspace>/api/2.0/omnigent"
            }
            aria-label="Automation API URL"
            data-testid="project-slack-automation-url"
          />
          <Button
            type="submit"
            size="sm"
            className="self-start"
            loading={save.isPending}
            disabled={busy || !canSave}
            data-testid="project-slack-save"
          >
            {!data?.linked ? "Link channel" : channelLink.trim() ? "Change channel" : "Save"}
          </Button>
        </form>
      )}
      {failure !== undefined && (
        <p className="text-destructive text-sm" role="alert">
          {failure.message}
        </p>
      )}
    </section>
  );
}
