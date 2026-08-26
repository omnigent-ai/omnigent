import { Excalidraw } from "@excalidraw/excalidraw";
import "@excalidraw/excalidraw/index.css";
import { useTheme } from "next-themes";
import { useMemo, type ComponentProps } from "react";
import { normalizeResolvedTheme } from "@/components/theme/themeMode";
import { parseExcalidrawScene } from "./codeViewerHelpers";
import { TruncatedBanner } from "./TruncatedBanner";

export function ExcalidrawViewer({
  content,
  truncated = false,
}: {
  content: string;
  truncated?: boolean;
}) {
  const { resolvedTheme } = useTheme();
  const result = useMemo(() => {
    try {
      return { scene: parseExcalidrawScene(content), error: null };
    } catch (error) {
      return {
        scene: null,
        error: error instanceof Error ? error.message : "Invalid Excalidraw file.",
      };
    }
  }, [content]);

  if (result.error || !result.scene) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-sm text-destructive">
        Unable to render diagram: {result.error}
      </div>
    );
  }

  const theme = normalizeResolvedTheme(resolvedTheme);
  return (
    <div data-testid="excalidraw-viewer" className="relative h-full min-h-0 overflow-hidden">
      {truncated && <TruncatedBanner />}
      <Excalidraw
        key={`${content.length}:${theme}`}
        initialData={result.scene as ComponentProps<typeof Excalidraw>["initialData"]}
        viewModeEnabled
        zenModeEnabled
        theme={theme}
        UIOptions={{ canvasActions: { loadScene: false, saveToActiveFile: false } }}
      />
    </div>
  );
}
