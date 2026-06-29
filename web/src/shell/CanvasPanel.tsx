/**
 * Right-rail Canvas panel — renders the conversation's agent-authored canvas
 * artifact (#2). HTML renders in a sandboxed iframe (scripts allowed, but no
 * same-origin / parent access, so an agent's HTML/JS widget runs without
 * touching the app); Markdown renders as preformatted source for now.
 */

import { useCanvas } from "@/hooks/useCanvas";

export function CanvasPanel({ conversationId }: { conversationId: string }) {
  const { data: canvas, isLoading, isError } = useCanvas(conversationId);

  if (isLoading) {
    return <div className="p-4 text-sm text-muted-foreground">Loading canvas…</div>;
  }
  if (isError) {
    return <div className="p-4 text-sm text-destructive">Failed to load canvas.</div>;
  }
  if (!canvas) {
    return (
      <div className="p-4 text-sm text-muted-foreground">
        No canvas yet. The agent can render one with the <code>set_canvas</code> tool.
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 border-b border-border px-3 py-1.5 text-xs font-medium text-muted-foreground">
        {canvas.title}
      </div>
      {canvas.content_type === "html" ? (
        <iframe
          title={canvas.title}
          srcDoc={canvas.content}
          // allow-scripts (no allow-same-origin): the artifact's JS runs but
          // can't reach the parent document, cookies, or storage.
          sandbox="allow-scripts"
          className="min-h-0 flex-1 border-0 bg-white"
        />
      ) : (
        <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap p-3 text-sm">
          {canvas.content}
        </pre>
      )}
    </div>
  );
}

export default CanvasPanel;
