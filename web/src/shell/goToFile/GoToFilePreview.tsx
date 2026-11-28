// Go to File's preview of the active row: the file read-only and highlighted as the file
// viewer shows it, scrolled to a line when one is given, such as a text search's match.

import { useMemo } from "react";

import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useFileContent } from "@/hooks/useFileContent";

import type { FilePosition } from "../FileViewerContext";

import { CodeMirrorFileViewer } from "../codeMirror/CodeMirrorFileViewer";
import { splitFilePath } from "./goToFileItems";

function PreviewMessage({ children }: { children: string }) {
  return (
    <div className="flex h-full items-center justify-center p-6 text-center text-ui text-muted-foreground">
      {children}
    </div>
  );
}

const noop = () => {};

/** How long the active row rests before its file is read. */
export const PREVIEW_DEBOUNCE_MS = 120;

/**
 * A picker's preview of the active file, read-only and highlighted as in the
 * file viewer, scrolled to and marking `line` when given, and the text up to
 * `endColumn` on it, such as a search match.
 */
export function GoToFilePreview({
  sessionId,
  path,
  line,
  column,
  endColumn,
}: {
  sessionId: string;
  path: string;
  line?: number;
  column?: number;
  endColumn?: number;
}) {
  // Stepping through rows reads only the file the active row settles on; the first shows at once.
  const target = useMemo(
    () => ({ path, line, column, endColumn }),
    [path, line, column, endColumn],
  );
  const shown = useDebouncedValue(target, PREVIEW_DEBOUNCE_MS);
  const file = useFileContent(sessionId, shown.path, { retry: false });
  // The viewer reveals each new position object once, so keep one per line.
  const position = useMemo<FilePosition | undefined>(
    () =>
      shown.line === undefined
        ? undefined
        : { line: shown.line, column: shown.column, endColumn: shown.endColumn },
    [shown],
  );
  const { name, folder } = splitFilePath(shown.path);
  if (file.isError) return <PreviewMessage>Couldn't load a preview of this file.</PreviewMessage>;
  if (!file.data) return <PreviewMessage>Loading preview…</PreviewMessage>;
  if (file.data.encoding !== "utf-8") {
    return <PreviewMessage>This file can't be previewed as text.</PreviewMessage>;
  }
  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="go-to-file-preview">
      <div className="flex shrink-0 items-baseline gap-2 border-b border-border px-3 py-1.5 text-xs">
        {/* The folder gives way first, then a long name; the line number stays visible. */}
        <span className="min-w-0 truncate font-medium text-foreground" title={shown.path}>
          {name}
        </span>
        {folder && (
          <span className="min-w-0 shrink-[1000] truncate text-muted-foreground" title={shown.path}>
            {folder}
          </span>
        )}
        {shown.line !== undefined && (
          <span className="ml-auto shrink-0 text-muted-foreground tabular-nums">
            Line {shown.line}
          </span>
        )}
      </div>
      <div className="min-h-0 flex-1">
        <CodeMirrorFileViewer
          key={shown.path}
          conversationId={sessionId}
          path={shown.path}
          content={file.data.content}
          position={position}
          truncated={file.data.truncated === true}
          wrapLines={false}
          searchOpen={false}
          onSearchHandled={noop}
          onSearchOpened={noop}
        />
      </div>
    </div>
  );
}
