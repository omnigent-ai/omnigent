// Live Edit's layer over a Browser tab's page: click text to open an editor where it is,
// see the change on the page as it's typed, then Discard it or send it to the agent.
import { Textarea } from "@/components/ui/textarea";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { cn } from "@/lib/utils";
import { runCommand, type CommandResult, type DesignCommand } from "./core/designCommands";
import { closeTool, useTabTools } from "./core/designToolsStore";
import { changed, LIVE_EDIT_MAX_TEXT, liveEditFor } from "./core/liveEdit";
import {
  EDITOR_CLASS,
  EditOutline,
  editorStyle,
  isSendShortcut,
  LAYER_Z,
  LiveEditDock,
} from "./layerParts";
import { scrollPage, sendOutcome, useToolSession, type LayerProps } from "./toolLayers";

export function LiveEditLayer({ tabId, url, rect, onNotice }: LayerProps) {
  const { edits, editing, caret, phase } = useTabTools(tabId);
  const frozen = phase !== "editing";
  const session = useToolSession(tabId, onNotice, true);
  const open = edits.find((edit) => edit.handle === editing) ?? null;
  const ready = edits.filter(changed);
  const actions = session ? liveEditFor(session, onNotice) : null;
  const closeEditor = () => session?.edit({ editing: null });

  const pick = (event: React.MouseEvent<HTMLDivElement>) => {
    if (actions && rect && !frozen) {
      void actions.pick(event.clientX - rect.left, event.clientY - rect.top);
    }
  };
  // The edits as this app has them, which the page can't change; the outbox sends them.
  const send = async (): Promise<CommandResult> => {
    const notice = actions ? actions.send(url) : null;
    if (notice) onNotice(notice);
    return sendOutcome(notice, "Live Edit");
  };
  const sendCommand: DesignCommand = {
    blocked: frozen
      ? "Live Edit is putting the page back."
      : ready.length === 0
        ? "Edit some text on the page first."
        : undefined,
    run: send,
  };

  if (!rect) return null;
  return (
    <>
      <div
        data-testid="live-edit-layer"
        className={cn(LAYER_Z, "fixed", frozen ? "cursor-wait" : "cursor-text")}
        style={{ left: rect.left, top: rect.top, width: rect.width, height: rect.height }}
        onClick={pick}
        onWheel={(event) => {
          // The editor sits where the text was; scrolling would leave it behind.
          if (open) closeEditor();
          if (session) scrollPage(session, event, rect);
        }}
      >
        {open && <EditOutline rect={open.rect} />}
      </div>
      {open && !frozen && (
        <Textarea
          key={open.handle}
          autoFocus
          value={open.after}
          maxLength={LIVE_EDIT_MAX_TEXT}
          aria-label="Edit text"
          onChange={(event) => actions?.type(open.handle, event.target.value)}
          onKeyDown={(event) => {
            if (isImeCompositionKeyEvent(event)) return;
            if (event.key === "Escape") {
              event.preventDefault();
              closeEditor();
            } else if (isSendShortcut(event)) {
              event.preventDefault();
              const result = runCommand(sendCommand);
              if (!result.ok) onNotice(result.error);
            } else if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              closeEditor();
            }
          }}
          onFocus={(event) => {
            // The cursor starts where the click landed in the text, else at its end.
            const field = event.currentTarget;
            const at = Math.min(caret ?? field.value.length, field.value.length);
            field.setSelectionRange(at, at);
          }}
          onBlur={closeEditor}
          className={cn(LAYER_Z, EDITOR_CLASS)}
          style={editorStyle(rect, open)}
        />
      )}
      <LiveEditDock
        rect={rect}
        ready={ready.length}
        discardDisabled={edits.length === 0 || frozen}
        sendDisabled={sendCommand.blocked !== undefined}
        onDiscard={() => void actions?.discard()}
        onSend={() => runCommand(sendCommand)}
        onClose={() => closeTool(tabId)}
      />
    </>
  );
}
