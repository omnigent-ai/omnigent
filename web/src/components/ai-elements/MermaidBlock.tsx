import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { Button } from "@/components/ui/button";
import { getEmbedRoot } from "@/lib/host";
import type { ComponentProps, ReactNode, RefObject } from "react";
import {
  cloneElement,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

// Streamdown portals fullscreen to document.body, outside the embed's scoped CSS.
export const MERMAID_NO_FULLSCREEN = { fullscreen: false } as const;

// Reproduced from Streamdown 2.5's own "View fullscreen" / "Exit fullscreen"
// glyphs so our button matches the download/copy icons in the same mermaid bar.
export const FullscreenIcon = (props: ComponentProps<"svg">) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <path
      clipRule="evenodd"
      d="M1 5.25V6H2.5V5.25V2.5H5.25H6V1H5.25H2C1.44772 1 1 1.44772 1 2V5.25ZM5.25 14.9994H6V13.4994H5.25H2.5V10.7494V9.99939H1V10.7494V13.9994C1 14.5517 1.44772 14.9994 2 14.9994H5.25ZM15 10V10.75V14C15 14.5523 14.5523 15 14 15H10.75H10V13.5H10.75H13.5V10.75V10H15ZM10.75 1H10V2.5H10.75H13.5V5.25V6H15V5.25V2C15 1.44772 14.5523 1 14 1H10.75Z"
      fill="currentColor"
      fillRule="evenodd"
    />
  </svg>
);

export function isMermaidPre(children: ReactNode): boolean {
  return (
    isValidElement<{ className?: string }>(children) &&
    (children.props.className?.split(/\s+/).includes("language-mermaid") ?? false)
  );
}

// The rendered diagram is Mermaid's own <svg id="mermaid-…"> — scope to it so
// we don't clone a toolbar/zoom-control icon (all 16×16 <svg>s in the block).
const DIAGRAM_SVG = 'svg[id^="mermaid-"]';
// A click while Mermaid is still drawing (e.g. as a reply settles) opens once it finishes.
export const DIAGRAM_WAIT_MS = 5_000;

export function extractCodeText(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") {
    return String(children);
  }

  if (Array.isArray(children)) {
    return children.map(extractCodeText).join("");
  }

  if (isValidElement(children)) {
    const props = children.props as { children?: ReactNode; code?: unknown };
    if (typeof props.code === "string") {
      return props.code;
    }
    return extractCodeText(props.children);
  }

  return "";
}

interface MermaidFullscreenControls {
  open: (block: HTMLElement, source: string) => void;
  close: () => void;
  track: (block: HTMLElement, source: string) => void;
  release: (block: HTMLElement) => void;
}

interface FullscreenDiagram {
  block: HTMLElement;
  source: string;
  // Null while the click waits for Mermaid to draw the diagram.
  svgMarkup: string | null;
  deadline: number;
}

// Holds the open diagram, or the click still waiting for Mermaid to draw it.
function useMermaidFullscreenState(resetKey?: string) {
  const [diagram, setDiagram] = useState<FullscreenDiagram | null>(null);
  const [stateKey, setStateKey] = useState(resetKey);
  if (stateKey !== resetKey) {
    setStateKey(resetKey);
    setDiagram(null);
  }

  // Reuse the sanitized inline SVG without rerunning Mermaid or copying toolbar icons.
  const open = useCallback((block: HTMLElement, source: string) => {
    const svgMarkup = block.querySelector(DIAGRAM_SVG)?.outerHTML ?? null;
    setDiagram({ block, source, svgMarkup, deadline: Date.now() + DIAGRAM_WAIT_MS });
  }, []);
  const close = useCallback(() => setDiagram(null), []);
  // When a settling reply remounts the clicked block, a new block with the same
  // source draws the same diagram, so it takes over.
  const track = useCallback((block: HTMLElement, source: string) => {
    setDiagram((current) => {
      if (current === null) return null;
      const replaced = !current.block.isConnected && current.source === source;
      return current.block === block || replaced ? { ...current, block, source } : current;
    });
  }, []);
  // Deferred past the commit's mount effects: only a same-commit replacement keeps the diagram.
  const release = useCallback((block: HTMLElement) => {
    queueMicrotask(() =>
      setDiagram((current) => (current?.block === block && !block.isConnected ? null : current)),
    );
  }, []);

  const waitingBlock = diagram?.svgMarkup === null ? diagram.block : null;
  const deadline = diagram?.deadline ?? 0;
  useEffect(() => {
    if (waitingBlock === null) return;
    const finish = (svgMarkup: string | null) =>
      setDiagram((current) => {
        if (current?.block !== waitingBlock || current.svgMarkup !== null) return current;
        return svgMarkup === null ? null : { ...current, svgMarkup };
      });
    const showWhenDrawn = () => {
      const svg = waitingBlock.isConnected ? waitingBlock.querySelector(DIAGRAM_SVG) : null;
      if (Date.now() >= deadline) finish(null);
      else if (svg) finish(svg.outerHTML);
    };
    const observer = new MutationObserver(showWhenDrawn);
    observer.observe(waitingBlock, { childList: true, subtree: true });
    const timer = window.setTimeout(() => finish(null), deadline - Date.now());
    showWhenDrawn();
    return () => {
      observer.disconnect();
      window.clearTimeout(timer);
    };
  }, [waitingBlock, deadline]);

  const active = diagram !== null;
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, close]);

  return { svgMarkup: diagram?.svgMarkup ?? null, open, close, track, release };
}

function MermaidFullscreenOverlay({
  svgMarkup,
  onClose,
}: {
  svgMarkup: string | null;
  onClose: () => void;
}) {
  if (svgMarkup === null) return null;
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/95 text-foreground backdrop-blur-sm"
      // Clicking the diagram must not dismiss it.
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="presentation"
    >
      <Button
        aria-label="Exit fullscreen"
        className="absolute top-4 right-4"
        onClick={onClose}
        size="icon-sm"
        title="Exit fullscreen"
        type="button"
        variant="ghost"
      >
        <FullscreenIcon />
      </Button>
      <div
        className="flex size-full items-center justify-center p-8 [&>svg]:h-auto [&>svg]:max-h-full [&>svg]:w-auto [&>svg]:max-w-full"
        // The cloned SVG is Mermaid's own trusted, already-sanitized render.
        dangerouslySetInnerHTML={{ __html: svgMarkup }}
        onClick={(e) => {
          if (e.target === e.currentTarget) onClose();
        }}
        role="presentation"
      />
    </div>,
    getEmbedRoot() ?? document.body,
  );
}

const MermaidFullscreenContext = createContext<MermaidFullscreenControls | null>(null);

/**
 * Owns the open diagram above the chat so it survives the block remount when a
 * streamed reply settles. Closes when the block unmounts unreplaced, or on
 * `resetKey` or theme change.
 */
export function MermaidFullscreenProvider({
  children,
  resetKey,
}: {
  children: ReactNode;
  resetKey: string;
}) {
  const themeMode = useResolvedThemeMode();
  const { svgMarkup, open, close, track, release } = useMermaidFullscreenState(
    `${resetKey}:${themeMode}`,
  );
  const controls = useMemo(() => ({ open, close, track, release }), [open, close, track, release]);
  return (
    <MermaidFullscreenContext.Provider value={controls}>
      {children}
      <MermaidFullscreenOverlay svgMarkup={svgMarkup} onClose={close} />
    </MermaidFullscreenContext.Provider>
  );
}

// Without a provider (isolated renders), the overlay lives with the block.
function MermaidFullscreen({
  blockRef,
  source,
}: {
  blockRef: RefObject<HTMLDivElement | null>;
  source: string;
}) {
  const shared = useContext(MermaidFullscreenContext);
  const local = useMermaidFullscreenState();
  const { open, track, release } = shared ?? local;

  useEffect(() => {
    if (blockRef.current) track(blockRef.current, source);
  }, [blockRef, source, track]);
  useEffect(() => {
    const block = blockRef.current;
    return () => {
      if (block) release(block);
    };
  }, [blockRef, release]);

  return (
    <>
      <Button
        aria-label="View fullscreen"
        className="size-6 bg-sidebar/80 text-muted-foreground hover:bg-sidebar/80 hover:text-foreground dark:hover:bg-sidebar/80 supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur"
        onClick={() => {
          if (blockRef.current) open(blockRef.current, source);
        }}
        size="icon-sm"
        title="View fullscreen"
        type="button"
        variant="ghost"
      >
        <FullscreenIcon />
      </Button>
      {shared === null && (
        <MermaidFullscreenOverlay svgMarkup={local.svgMarkup} onClose={local.close} />
      )}
    </>
  );
}

// The .chat-mermaid rule reserves a slot beside Streamdown's download/copy toolbar.
export function MermaidBlock({ block }: { block: ReactNode }) {
  const blockRef = useRef<HTMLDivElement>(null);
  return (
    <div ref={blockRef} className="chat-mermaid relative">
      {block}
      <div className="absolute top-2 right-2 z-20 flex items-center rounded-md border border-sidebar bg-sidebar/80 px-1.5 py-1 supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur">
        <MermaidFullscreen blockRef={blockRef} source={extractCodeText(block)} />
      </div>
    </div>
  );
}

function MermaidPre({ children }: ComponentProps<"pre">) {
  // Preserve Streamdown's default handling of ordinary code fences.
  const block = isValidElement(children)
    ? cloneElement(children, { "data-block": "true" } as Record<string, unknown>)
    : children;
  return isMermaidPre(children) ? <MermaidBlock block={block} /> : block;
}

// Keep the replacement toolbar and disabled native fullscreen together on every surface.
export const MERMAID_STREAMDOWN_PROPS = {
  components: { pre: MermaidPre },
  controls: { mermaid: MERMAID_NO_FULLSCREEN },
};
