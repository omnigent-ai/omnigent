// A thingy with its controls and comments, as a session's side panel and New session show it.
// Its actions, quotes and addressed comments go to `sessionId` when it is shown beside a
// session, and otherwise to the viewer's thingy session.
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { InfoIcon, Loader2Icon, TriangleAlertIcon } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { CommentsPanel } from "@/shell/CommentsPanel";
import { type Thingy, type ThingyContent, getThingyContent } from "@/thingies/api/thingyApi";
import { ThingyFrame } from "@/thingies/host/ThingyFrame";
import { openThingySession } from "@/thingies/host/thingySession";
import { THINGY_SDK_VERSION } from "@/thingies/sdk/thingyProtocol";
import { ThingyBuildingState } from "./ThingyBuildingState";
import { ThingyErrorNotice } from "./ThingyErrorNotice";
import { ThingyCommentsButton } from "./ThingyToolbar";
import { thingyErrorMessage, useThingyNavigate } from "./thingyNavigation";
import { THINGIES_KEY } from "./thingyQueries";
import { useThingyComments } from "./useThingyComments";

function thingyContentKey(thingyId: string, revision: string) {
  return [...THINGIES_KEY, "content", thingyId, revision] as const;
}

function permissionsKey({ permissions }: Thingy): string {
  return [permissions.read, permissions.write_data, permissions.publish, permissions.manage]
    .map(Number)
    .join("");
}

function PublishedThingy({
  thingy,
  revision,
  sessionId,
  onReloadThingy,
  onContentLoaded,
  onAddComment,
}: {
  thingy: Thingy;
  revision: string;
  sessionId?: string;
  onReloadThingy: () => void;
  onContentLoaded?: (content: ThingyContent) => void;
  onAddComment?: Parameters<typeof ThingyFrame>[0]["onAddComment"];
}) {
  const content = useQuery({
    queryKey: thingyContentKey(thingy.id, revision),
    queryFn: ({ signal }) => getThingyContent(thingy.id, signal),
    staleTime: Infinity,
  });
  const [revisionChanged, setRevisionChanged] = useState(false);
  const onRevisionChanged = useCallback(() => setRevisionChanged(true), []);
  useEffect(() => {
    if (content.data) onContentLoaded?.(content.data);
  }, [content.data, onContentLoaded]);

  if (content.isPending) {
    return (
      <div className="flex min-h-60 flex-1 items-center justify-center gap-2 text-ui text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        Loading thingy…
      </div>
    );
  }
  if (content.isError) {
    return (
      <ThingyErrorNotice
        className="m-3"
        title="Couldn’t load the thingy"
        error={content.error}
        onRetry={() => void content.refetch()}
      />
    );
  }
  if (content.data.sdk_version !== THINGY_SDK_VERSION) {
    return (
      <ThingyErrorNotice
        className="m-3"
        title="This thingy needs a newer version of Omnigent"
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
          <span className="flex-1">A newer version of this thingy was published.</span>
          <Button
            variant="outline"
            size="sm"
            componentId="thingies.load_new_version"
            onClick={() => {
              setRevisionChanged(false);
              onReloadThingy();
            }}
          >
            Load new version
          </Button>
        </div>
      )}
      <ThingyFrame
        // A change to the viewer's access starts the page again, with what it may do now.
        key={`${content.data.revision_id}:${permissionsKey(thingy)}`}
        app={thingy}
        content={content.data}
        onRevisionChanged={onRevisionChanged}
        sessionId={sessionId}
        onAddComment={onAddComment}
      />
    </div>
  );
}

export interface ThingyViewProps {
  thingy: Thingy;
  /** The session it is shown beside, in that session's side panel; none beside New session. */
  sessionId?: string;
  /** Reloads the thingy, after it publishes a version or its sharing changes. */
  onReloadThingy: () => void;
  /** The thingy's controls, given the comments toggle to place among them. */
  renderToolbar: (commentsButton: ReactNode) => ReactNode;
  /** Where the controls go instead of above the thingy: the side panel's bar in full screen. */
  toolbarTarget: HTMLElement | null;
  onContentLoaded?: (content: ThingyContent) => void;
  /** Whether it fills the screen, with its session out of sight. */
  fullScreen?: boolean;
  /** Shows the session it is shown with, leaving full screen. */
  onOpenSession?: () => void;
}

export function ThingyView({
  thingy,
  sessionId,
  onReloadThingy,
  renderToolbar,
  toolbarTarget,
  onContentLoaded,
  fullScreen = false,
  onOpenSession,
}: ThingyViewProps) {
  const navigate = useThingyNavigate();
  const queryClient = useQueryClient();
  const address = useCallback(
    async (message: string) => {
      await openThingySession(thingy, message, navigate, {
        deliver: true,
        reveal: false,
        railSessionId: sessionId,
      });
    },
    [navigate, sessionId, thingy],
  );
  const comments = useThingyComments(thingy, thingy.permissions.publish ? address : undefined);
  const controls = (
    <div className="flex min-w-0 flex-1 items-center gap-1">
      {renderToolbar(<ThingyCommentsButton comments={comments} />)}
    </div>
  );
  const reload = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: [...THINGIES_KEY, "content", thingy.id] });
    onReloadThingy();
  }, [onReloadThingy, queryClient, thingy.id]);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {toolbarTarget ? (
        createPortal(controls, toolbarTarget)
      ) : (
        <div
          role="toolbar"
          aria-label={`${thingy.name} controls`}
          className="flex shrink-0 items-center gap-1 border-b border-border px-4 py-1.5"
        >
          {controls}
        </div>
      )}
      {comments.error !== undefined && (
        <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5 text-sm text-destructive">
          <TriangleAlertIcon className="size-3.5 shrink-0" />
          <span className="min-w-0 truncate">
            Couldn’t update the comments: {thingyErrorMessage(comments.error)}
          </span>
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          {thingy.published_revision ? (
            <PublishedThingy
              key={thingy.published_revision}
              thingy={thingy}
              revision={thingy.published_revision}
              sessionId={sessionId}
              onReloadThingy={reload}
              onContentLoaded={onContentLoaded}
              onAddComment={comments.canComment ? comments.startComment : undefined}
            />
          ) : (
            <ThingyBuildingState
              thingyId={thingy.id}
              canPublish={thingy.permissions.publish}
              besideSession={sessionId !== undefined}
              onOpenSession={fullScreen ? onOpenSession : undefined}
              onPublished={onReloadThingy}
            />
          )}
        </div>
        {comments.open && <CommentsPanel {...comments.panel} />}
      </div>
    </div>
  );
}
