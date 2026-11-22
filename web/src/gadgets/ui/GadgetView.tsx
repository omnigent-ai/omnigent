// A gadget with its controls and comments, as a session's side panel and New session show it.
// Its actions, quotes and addressed comments go to `sessionId` when it is shown beside a
// session, and otherwise to the viewer's gadget session.
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { InfoIcon, Loader2Icon, TriangleAlertIcon } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { CommentsPanel } from "@/shell/CommentsPanel";
import { type Gadget, type GadgetContent, getGadgetContent } from "@/gadgets/api/gadgetApi";
import { GadgetFrame } from "@/gadgets/host/GadgetFrame";
import { openGadgetSession } from "@/gadgets/host/gadgetSession";
import { GADGET_SDK_VERSION } from "@/gadgets/sdk/gadgetProtocol";
import { GadgetBuildingState } from "./GadgetBuildingState";
import { GadgetErrorNotice } from "./GadgetErrorNotice";
import { GadgetCommentsButton } from "./GadgetToolbar";
import { gadgetErrorMessage, useGadgetNavigate } from "./gadgetNavigation";
import { GADGETS_KEY } from "./gadgetQueries";
import { useGadgetComments } from "./useGadgetComments";

function gadgetContentKey(gadgetId: string, revision: string) {
  return [...GADGETS_KEY, "content", gadgetId, revision] as const;
}

function permissionsKey({ permissions }: Gadget): string {
  return [permissions.read, permissions.write_data, permissions.publish, permissions.manage]
    .map(Number)
    .join("");
}

function PublishedGadget({
  gadget,
  revision,
  sessionId,
  onReloadGadget,
  onContentLoaded,
  onAddComment,
}: {
  gadget: Gadget;
  revision: string;
  sessionId?: string;
  onReloadGadget: () => void;
  onContentLoaded?: (content: GadgetContent) => void;
  onAddComment?: Parameters<typeof GadgetFrame>[0]["onAddComment"];
}) {
  const content = useQuery({
    queryKey: gadgetContentKey(gadget.id, revision),
    queryFn: ({ signal }) => getGadgetContent(gadget.id, signal),
    staleTime: Infinity,
  });
  const [revisionChanged, setRevisionChanged] = useState(false);
  // The page learned that a newer revision is published: load it now rather than at the next
  // poll, so the page does not sit on errors about a revision that is gone.
  const onRevisionChanged = useCallback(() => {
    setRevisionChanged(true);
    onReloadGadget();
  }, [onReloadGadget]);
  useEffect(() => {
    if (content.data) onContentLoaded?.(content.data);
  }, [content.data, onContentLoaded]);

  if (content.isPending) {
    return (
      <div className="flex min-h-60 flex-1 items-center justify-center gap-2 text-ui text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        Loading gadget…
      </div>
    );
  }
  if (content.isError) {
    return (
      <GadgetErrorNotice
        className="m-3"
        title="Couldn’t load the gadget"
        error={content.error}
        onRetry={() => void content.refetch()}
      />
    );
  }
  if (content.data.sdk_version !== GADGET_SDK_VERSION) {
    return (
      <GadgetErrorNotice
        className="m-3"
        title="This gadget needs a newer version of Omnigent"
        error="Its page was written for an SDK this app doesn’t support yet."
      />
    );
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {revisionChanged && (
        <div
          role="status"
          className="flex shrink-0 items-center gap-2 border-b border-border bg-muted/40 px-3 py-2 text-ui"
        >
          <InfoIcon className="size-4 shrink-0 text-muted-foreground" />
          <span className="flex-1">A newer version of this gadget was published. Loading it…</span>
          <Button
            variant="outline"
            size="sm"
            componentId="gadgets.load_new_version"
            onClick={() => {
              setRevisionChanged(false);
              onReloadGadget();
            }}
          >
            Load new version
          </Button>
        </div>
      )}
      <GadgetFrame
        // A change to the viewer's access starts the page again, with what it may do now.
        key={`${content.data.revision_id}:${permissionsKey(gadget)}`}
        app={gadget}
        content={content.data}
        onRevisionChanged={onRevisionChanged}
        sessionId={sessionId}
        onAddComment={onAddComment}
      />
    </div>
  );
}

export interface GadgetViewProps {
  gadget: Gadget;
  /** The session it is shown beside, in that session's side panel; none beside New session. */
  sessionId?: string;
  /** Reloads the gadget, after it publishes a version or its sharing changes. */
  onReloadGadget: () => void;
  /** The gadget's controls, given the comments toggle to place among them. */
  renderToolbar: (commentsButton: ReactNode) => ReactNode;
  /** Where the controls go instead of above the gadget: the side panel's bar in full screen. */
  toolbarTarget: HTMLElement | null;
  onContentLoaded?: (content: GadgetContent) => void;
  /** Whether it fills the screen, with its session out of sight. */
  fullScreen?: boolean;
  /** Shows the session it is shown with, leaving full screen. */
  onOpenSession?: () => void;
}

export function GadgetView({
  gadget,
  sessionId,
  onReloadGadget,
  renderToolbar,
  toolbarTarget,
  onContentLoaded,
  fullScreen = false,
  onOpenSession,
}: GadgetViewProps) {
  const navigate = useGadgetNavigate();
  const queryClient = useQueryClient();
  const address = useCallback(
    async (message: string) => {
      await openGadgetSession(gadget, message, navigate, {
        deliver: true,
        reveal: false,
        railSessionId: sessionId,
      });
    },
    [navigate, sessionId, gadget],
  );
  const comments = useGadgetComments(gadget, gadget.permissions.publish ? address : undefined);
  const controls = (
    <div className="flex min-w-0 flex-1 items-center gap-1">
      {renderToolbar(<GadgetCommentsButton comments={comments} />)}
    </div>
  );
  const reload = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: [...GADGETS_KEY, "content", gadget.id] });
    onReloadGadget();
  }, [onReloadGadget, queryClient, gadget.id]);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {toolbarTarget ? (
        createPortal(controls, toolbarTarget)
      ) : (
        <div
          role="toolbar"
          aria-label={`${gadget.name} controls`}
          className="flex shrink-0 items-center gap-1 border-b border-border px-4 py-1.5"
        >
          {controls}
        </div>
      )}
      {comments.error !== undefined && (
        <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5 text-sm text-destructive">
          <TriangleAlertIcon className="size-3.5 shrink-0" />
          <span className="min-w-0 truncate">
            Couldn’t update the comments: {gadgetErrorMessage(comments.error)}
          </span>
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          {gadget.published_revision ? (
            <PublishedGadget
              key={gadget.published_revision}
              gadget={gadget}
              revision={gadget.published_revision}
              sessionId={sessionId}
              onReloadGadget={reload}
              onContentLoaded={onContentLoaded}
              onAddComment={comments.canComment ? comments.startComment : undefined}
            />
          ) : (
            <GadgetBuildingState
              gadgetId={gadget.id}
              canPublish={gadget.permissions.publish}
              besideSession={sessionId !== undefined}
              onOpenSession={fullScreen ? onOpenSession : undefined}
              onPublished={onReloadGadget}
            />
          )}
        </div>
        {comments.open && <CommentsPanel {...comments.panel} />}
      </div>
    </div>
  );
}
