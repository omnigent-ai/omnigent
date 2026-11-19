import { SideChatPane } from "@/components/chat/SideChatPane";
import { useCanEdit } from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { readOnlyReasonForSessionLabels } from "@/lib/sessionCapabilities";

/** An existing session's full history and its own composer, read-only when it can't take turns. */
export function FullSessionChat({
  sessionId,
  evenComposerPadding = false,
  transcriptTopInset,
}: {
  sessionId: string;
  /** Space below the composer matches its sides, for a pane that frames it on all edges. */
  evenComposerPadding?: boolean;
  /** Extra room above the transcript, as a CSS length, for a header laid over it. */
  transcriptTopInset?: string;
}) {
  const { session } = useSession(sessionId);
  const canEdit = useCanEdit(sessionId);
  const readOnlyReason = readOnlyReasonForSessionLabels(session, null);
  return (
    <>
      <div className="min-h-0 flex-1">
        <SideChatPane
          childId={sessionId}
          fullSession
          evenComposerPadding={evenComposerPadding}
          transcriptTopInset={transcriptTopInset}
          readOnly={!canEdit || session === null || readOnlyReason !== null}
        />
      </div>
      {readOnlyReason && (
        <p className="px-4 py-2 text-sm text-muted-foreground">{readOnlyReason}</p>
      )}
    </>
  );
}
