import { lazy, Suspense, type ComponentProps } from "react";
import type { FileViewer as FileViewerImpl } from "./FileViewer";

// `FileViewer` is the only reachable path to the TipTap / ProseMirror rich-text
// stack, so it stays out of the entry chunk until a file is actually opened.
const FileViewerChunk = lazy(() => import("./FileViewer").then((m) => ({ default: m.FileViewer })));

export type FileViewerProps = ComponentProps<typeof FileViewerImpl>;

/** `FileViewer`, loaded on demand behind the same "Loading…" panel `CodeViewer` uses. */
export function LazyFileViewer(props: FileViewerProps) {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center p-8 text-muted-foreground text-ui">
          Loading…
        </div>
      }
    >
      <FileViewerChunk {...props} />
    </Suspense>
  );
}
