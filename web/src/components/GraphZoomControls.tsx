import { useReactFlow } from "@xyflow/react";
import { Maximize2Icon, ZoomInIcon, ZoomOutIcon } from "lucide-react";

/** Zoom in, zoom out and fit, in the corner of a React Flow graph. */
export function GraphZoomControls() {
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const btnClass =
    "flex items-center justify-center size-7 rounded-md border bg-card text-muted-foreground shadow-sm hover:bg-accent hover:text-accent-foreground transition-colors";
  return (
    <div className="absolute bottom-3 right-3 z-10 flex flex-col gap-1">
      <button
        type="button"
        className={btnClass}
        onClick={() => zoomIn({ duration: 200 })}
        aria-label="Zoom in"
      >
        <ZoomInIcon className="size-4" />
      </button>
      <button
        type="button"
        className={btnClass}
        onClick={() => zoomOut({ duration: 200 })}
        aria-label="Zoom out"
      >
        <ZoomOutIcon className="size-4" />
      </button>
      <button
        type="button"
        className={btnClass}
        onClick={() => fitView({ duration: 200, padding: 0.3 })}
        aria-label="Fit view"
      >
        <Maximize2Icon className="size-4" />
      </button>
    </div>
  );
}
