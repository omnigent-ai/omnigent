"use client";

import { Button } from "@/components/ui/button";
import { ButtonGroup, ButtonGroupText } from "@/components/ui/button-group";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { copyText } from "@/lib/clipboard";
import { getEmbedRoot } from "@/lib/host";
import { cn } from "@/lib/utils";
import type { UIMessage } from "ai";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import type { ComponentProps, HTMLAttributes, ReactElement, ReactNode, RefObject } from "react";
import {
  cloneElement,
  createContext,
  isValidElement,
  memo,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Streamdown, type StreamdownProps } from "streamdown";

import { MarkdownErrorBoundary } from "./MarkdownErrorBoundary";

import {
  CHAT_LINK_SAFETY,
  FILE_LINK_STREAMDOWN_REHYPE_PLUGINS,
  SECURE_STREAMDOWN_REHYPE_PLUGINS,
  STREAMDOWN_PLUGINS,
} from "./streamdown-security";

export type MessageProps = HTMLAttributes<HTMLDivElement> & {
  from: UIMessage["role"];
};

export const Message = ({ className, from, ...props }: MessageProps) => (
  <div
    className={cn(
      // min-w-0 lets this flex item shrink below its content's intrinsic width instead of widening the column.
      "group flex w-full min-w-0 max-w-[95%] flex-col gap-2",
      from === "user" ? "is-user ml-auto justify-end" : "is-assistant",
      className,
    )}
    {...props}
  />
);

export type MessageContentProps = HTMLAttributes<HTMLDivElement>;

export const MessageContent = ({ children, className, ...props }: MessageContentProps) => (
  <div
    className={cn(
      // User and assistant prose share the settings-driven interface text
      // token, so their size and line-height stay in lockstep.
      "is-user:dark flex w-fit min-w-0 max-w-full flex-col gap-2 text-ui",
      "group-[.is-user]:ml-auto group-[.is-user]:overflow-hidden group-[.is-user]:rounded-2xl group-[.is-user]:bg-muted group-[.is-user]:px-3 group-[.is-user]:py-2 group-[.is-user]:text-foreground group-[.is-user]:ring-1 group-[.is-user]:ring-border/60",
      // Tighter than the user bubble's gap-2 so muted single-line tool
      // ("See N steps") / reasoning rows don't look orphaned between prose.
      "group-[.is-assistant]:gap-1.5 group-[.is-assistant]:text-foreground",
      className,
    )}
    {...props}
  >
    {children}
  </div>
);

export type MessageActionsProps = ComponentProps<"div">;

export const MessageActions = ({ className, children, ...props }: MessageActionsProps) => (
  <div className={cn("flex items-center gap-3", className)} {...props}>
    {children}
  </div>
);

export type MessageActionProps = ComponentProps<typeof Button> & {
  tooltip?: string;
  label?: string;
};

export const MessageAction = ({
  tooltip,
  children,
  label,
  className,
  variant = "ghost",
  size = "icon-sm",
  ...props
}: MessageActionProps) => {
  const button = (
    <Button
      size={size}
      type="button"
      variant={variant}
      className={cn("text-muted-foreground hover:text-foreground", className)}
      {...props}
    >
      {children}
      <span className="sr-only">{label || tooltip}</span>
    </Button>
  );

  if (tooltip) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>{button}</TooltipTrigger>
          <TooltipContent>
            <p>{tooltip}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return button;
};

interface MessageBranchContextType {
  currentBranch: number;
  totalBranches: number;
  goToPrevious: () => void;
  goToNext: () => void;
  branches: ReactElement[];
  setBranches: (branches: ReactElement[]) => void;
}

const MessageBranchContext = createContext<MessageBranchContextType | null>(null);

const useMessageBranch = () => {
  const context = useContext(MessageBranchContext);

  if (!context) {
    throw new Error("MessageBranch components must be used within MessageBranch");
  }

  return context;
};

export type MessageBranchProps = HTMLAttributes<HTMLDivElement> & {
  defaultBranch?: number;
  onBranchChange?: (branchIndex: number) => void;
};

export const MessageBranch = ({
  defaultBranch = 0,
  onBranchChange,
  className,
  ...props
}: MessageBranchProps) => {
  const [currentBranch, setCurrentBranch] = useState(defaultBranch);
  const [branches, setBranches] = useState<ReactElement[]>([]);

  const handleBranchChange = useCallback(
    (newBranch: number) => {
      setCurrentBranch(newBranch);
      onBranchChange?.(newBranch);
    },
    [onBranchChange],
  );

  const goToPrevious = useCallback(() => {
    const newBranch = currentBranch > 0 ? currentBranch - 1 : branches.length - 1;
    handleBranchChange(newBranch);
  }, [currentBranch, branches.length, handleBranchChange]);

  const goToNext = useCallback(() => {
    const newBranch = currentBranch < branches.length - 1 ? currentBranch + 1 : 0;
    handleBranchChange(newBranch);
  }, [currentBranch, branches.length, handleBranchChange]);

  const contextValue = useMemo<MessageBranchContextType>(
    () => ({
      branches,
      currentBranch,
      goToNext,
      goToPrevious,
      setBranches,
      totalBranches: branches.length,
    }),
    [branches, currentBranch, goToNext, goToPrevious],
  );

  return (
    <MessageBranchContext.Provider value={contextValue}>
      <div className={cn("grid w-full gap-2 [&>div]:pb-0", className)} {...props} />
    </MessageBranchContext.Provider>
  );
};

export type MessageBranchContentProps = HTMLAttributes<HTMLDivElement>;

export const MessageBranchContent = ({ children, ...props }: MessageBranchContentProps) => {
  const { currentBranch, setBranches, branches } = useMessageBranch();
  const childrenArray = useMemo(
    () => (Array.isArray(children) ? children : [children]),
    [children],
  );

  // Use useEffect to update branches when they change
  useEffect(() => {
    if (branches.length !== childrenArray.length) {
      setBranches(childrenArray);
    }
  }, [childrenArray, branches, setBranches]);

  return childrenArray.map((branch, index) => (
    <div
      className={cn(
        "grid gap-2 overflow-hidden [&>div]:pb-0",
        index === currentBranch ? "block" : "hidden",
      )}
      key={branch.key}
      {...props}
    >
      {branch}
    </div>
  ));
};

export type MessageBranchSelectorProps = ComponentProps<typeof ButtonGroup>;

export const MessageBranchSelector = ({ className, ...props }: MessageBranchSelectorProps) => {
  const { totalBranches } = useMessageBranch();

  // Don't render if there's only one branch
  if (totalBranches <= 1) {
    return null;
  }

  return (
    <ButtonGroup
      className={cn(
        "[&>*:not(:first-child)]:rounded-l-md [&>*:not(:last-child)]:rounded-r-md",
        className,
      )}
      orientation="horizontal"
      {...props}
    />
  );
};

export type MessageBranchPreviousProps = ComponentProps<typeof Button>;

export const MessageBranchPrevious = ({ children, ...props }: MessageBranchPreviousProps) => {
  const { goToPrevious, totalBranches } = useMessageBranch();

  return (
    <Button
      aria-label="Previous branch"
      disabled={totalBranches <= 1}
      onClick={goToPrevious}
      size="icon-sm"
      type="button"
      variant="ghost"
      {...props}
    >
      {children ?? <ChevronLeftIcon size={14} />}
    </Button>
  );
};

export type MessageBranchNextProps = ComponentProps<typeof Button>;

export const MessageBranchNext = ({ children, ...props }: MessageBranchNextProps) => {
  const { goToNext, totalBranches } = useMessageBranch();

  return (
    <Button
      aria-label="Next branch"
      disabled={totalBranches <= 1}
      onClick={goToNext}
      size="icon-sm"
      type="button"
      variant="ghost"
      {...props}
    >
      {children ?? <ChevronRightIcon size={14} />}
    </Button>
  );
};

export type MessageBranchPageProps = HTMLAttributes<HTMLSpanElement>;

export const MessageBranchPage = ({ className, ...props }: MessageBranchPageProps) => {
  const { currentBranch, totalBranches } = useMessageBranch();

  return (
    <ButtonGroupText
      className={cn("border-none bg-transparent text-muted-foreground shadow-none", className)}
      {...props}
    >
      {currentBranch + 1} of {totalBranches}
    </ButtonGroupText>
  );
};

export type MessageResponseProps = Omit<StreamdownProps, "rehypePlugins"> & {
  /**
   * Hand file-path links to the `a` component override instead of letting the
   * harden pass turn them into app-origin navigations or " [blocked]" text.
   * Opt-in: only callers that supply that override may set it.
   */
  markFileLinks?: boolean;
};

// Streamdown's mermaid fullscreen portals its overlay to `document.body`, which
// in the embed sits outside the `.omnigent-app` theme scope, so the overlay
// renders unstyled/invisible. Disable it and render our own fullscreen (portaled
// via `getEmbedRoot()`, see MermaidFullscreen) instead. Download/copy/panZoom
// keep Streamdown's defaults.
const MERMAID_NO_FULLSCREEN = { fullscreen: false } as const;

function getChatCodeControls(controls: StreamdownProps["controls"]): StreamdownProps["controls"] {
  if (typeof controls === "object" && controls !== null) {
    const codeControls = controls.code;
    const mermaidControls = controls.mermaid;
    return {
      ...controls,
      code: {
        ...(typeof codeControls === "object" && codeControls !== null ? codeControls : {}),
        copy: false,
        download: true,
      },
      mermaid: {
        ...(typeof mermaidControls === "object" && mermaidControls !== null ? mermaidControls : {}),
        ...MERMAID_NO_FULLSCREEN,
      },
    };
  }

  return { code: { copy: false, download: true }, mermaid: MERMAID_NO_FULLSCREEN };
}

function extractCodeText(children: ReactNode): string {
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

// Streamdown ships its own 16px icon set (filled, even-odd fill rule) rather
// than lucide, so the copy/wrap buttons we overlay on a code block header use
// the same glyphs as Streamdown's native buttons (e.g. copy/download in a
// table) instead of lucide look-alikes that render at a different weight. The
// copy and check paths are reproduced from Streamdown 2.5; Streamdown has no
// wrap-text icon of its own, so that glyph is drawn to match its style (a
// 16-viewBox stroke, as Streamdown uses for its own stroked icons). They render
// through our shared Button, whose `button-standard-icons` rule normalizes them
// to the standard icon box — the size of Streamdown's own table copy button.
type CodeHeaderIconProps = ComponentProps<"svg">;

const CodeCopyIcon = (props: CodeHeaderIconProps) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <path
      clipRule="evenodd"
      d="M2.75 0.5C1.7835 0.5 1 1.2835 1 2.25V9.75C1 10.7165 1.7835 11.5 2.75 11.5H3.75H4.5V10H3.75H2.75C2.61193 10 2.5 9.88807 2.5 9.75V2.25C2.5 2.11193 2.61193 2 2.75 2H8.25C8.38807 2 8.5 2.11193 8.5 2.25V3H10V2.25C10 1.2835 9.2165 0.5 8.25 0.5H2.75ZM7.75 4.5C6.7835 4.5 6 5.2835 6 6.25V13.75C6 14.7165 6.7835 15.5 7.75 15.5H13.25C14.2165 15.5 15 14.7165 15 13.75V6.25C15 5.2835 14.2165 4.5 13.25 4.5H7.75ZM7.5 6.25C7.5 6.11193 7.61193 6 7.75 6H13.25C13.3881 6 13.5 6.11193 13.5 6.25V13.75C13.5 13.8881 13.3881 14 13.25 14H7.75C7.61193 14 7.5 13.8881 7.5 13.75V6.25Z"
      fill="currentColor"
      fillRule="evenodd"
    />
  </svg>
);

const CodeCheckIcon = (props: CodeHeaderIconProps) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <path
      clipRule="evenodd"
      d="M15.5607 3.99999L15.0303 4.53032L6.23744 13.3232C5.55403 14.0066 4.44599 14.0066 3.76257 13.3232L4.2929 12.7929L3.76257 13.3232L0.969676 10.5303L0.439346 9.99999L1.50001 8.93933L2.03034 9.46966L4.82323 12.2626C4.92086 12.3602 5.07915 12.3602 5.17678 12.2626L13.9697 3.46966L14.5 2.93933L15.5607 3.99999Z"
      fill="currentColor"
      fillRule="evenodd"
    />
  </svg>
);

// Reproduced from Streamdown 2.5's own "View fullscreen" / "Exit fullscreen"
// glyphs so our button matches the download/copy icons in the same mermaid bar.
const CodeFullscreenIcon = (props: CodeHeaderIconProps) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <path
      clipRule="evenodd"
      d="M1 5.25V6H2.5V5.25V2.5H5.25H6V1H5.25H2C1.44772 1 1 1.44772 1 2V5.25ZM5.25 14.9994H6V13.4994H5.25H2.5V10.7494V9.99939H1V10.7494V13.9994C1 14.5517 1.44772 14.9994 2 14.9994H5.25ZM15 10V10.75V14C15 14.5523 14.5523 15 14 15H10.75H10V13.5H10.75H13.5V10.75V10H15ZM10.75 1H10V2.5H10.75H13.5V5.25V6H15V5.25V2C15 1.44772 14.5523 1 14 1H10.75Z"
      fill="currentColor"
      fillRule="evenodd"
    />
  </svg>
);

const CodeWrapTextIcon = (props: CodeHeaderIconProps) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <g stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.75}>
      <path d="M2 4H14" />
      <path d="M2 8H11A2 2 0 1 1 11 12H8.5" />
      <path d="M10 10.5 8.5 12 10 13.5" />
      <path d="M2 12H5.5" />
    </g>
  </svg>
);

// Shared visual style for the buttons in a chat code block header (copy, wrap
// toggle). Keeps the frosted resting look that matches Streamdown's own button
// pill, but pins the hover background to that same frosted fill so the ghost
// variant's grey hover box never appears — on hover only the icon brightens,
// exactly like Streamdown's built-in buttons (e.g. download). Positioning lives
// on the container in ChatCodeBlockPre, so the buttons stay layout-agnostic.
const CODE_BLOCK_OVERLAY_BUTTON_CLASS =
  "size-6 bg-sidebar/80 text-muted-foreground hover:bg-sidebar/80 hover:text-foreground dark:hover:bg-sidebar/80 supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur";

function ChatCodeBlockCopyButton({ getCode }: { getCode: () => string }) {
  const [isCopied, setIsCopied] = useState(false);
  const timeoutRef = useRef<number>(0);

  const handleClick = useCallback(() => {
    if (isCopied) return;

    try {
      const copyResult = copyText(getCode());
      void copyResult.then(
        () => {
          setIsCopied(true);
          timeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
        },
        (error) => {
          console.warn("Failed to copy code block", error);
        },
      );
    } catch (error) {
      console.warn("Failed to copy code block", error);
    }
  }, [getCode, isCopied]);

  useEffect(
    () => () => {
      window.clearTimeout(timeoutRef.current);
    },
    [],
  );

  const Icon = isCopied ? CodeCheckIcon : CodeCopyIcon;

  return (
    <Button
      aria-label="Copy Code"
      className={CODE_BLOCK_OVERLAY_BUTTON_CLASS}
      onClick={handleClick}
      size="icon-sm"
      title="Copy Code"
      type="button"
      variant="ghost"
    >
      <Icon />
    </Button>
  );
}

function ChatCodeBlockWrapToggle({ wrap, onToggle }: { wrap: boolean; onToggle: () => void }) {
  return (
    <Button
      aria-label="Toggle word wrap"
      // aria-pressed carries the on/off state; the icon stays muted like the
      // sibling buttons rather than brightening to foreground when active.
      aria-pressed={wrap}
      className={CODE_BLOCK_OVERLAY_BUTTON_CLASS}
      onClick={onToggle}
      size="icon-sm"
      title={wrap ? "Disable word wrap" : "Enable word wrap"}
      type="button"
      variant="ghost"
    >
      <CodeWrapTextIcon />
    </Button>
  );
}

// A mermaid fence renders as a diagram with Streamdown's own toolbar
// (download / copy / fullscreen), so our wrap+copy overlay is meaningless there
// (word wrap does nothing to an SVG, copy duplicates) and its buttons collide
// with that toolbar. Detect it the same way CodeViewer does — the `<code>` child
// carries `language-mermaid` — and skip the overlay for those blocks.
function isMermaidPre(children: ReactNode): boolean {
  return (
    isValidElement<{ className?: string }>(children) &&
    (children.props.className?.split(/\s+/).includes("language-mermaid") ?? false)
  );
}

// Full-screen view of a mermaid diagram. Streamdown's own fullscreen is disabled
// (see getChatCodeControls) because it portals its overlay to `document.body`,
// outside the embed's `.omnigent-app` theme scope, where it renders unstyled.
// We clone the already-rendered inline SVG into our own overlay portaled to
// `getEmbedRoot() ?? document.body` — the same target CodeViewer uses — so it
// inherits theme tokens embedded and standalone alike. Cloning the live SVG
// (rather than re-rendering through Streamdown) avoids re-running Mermaid and
// keeps the block chrome (label, borders, toolbar) out of the fullscreen view.
// Escape or a backdrop click closes it.
function MermaidFullscreen({ blockRef }: { blockRef: RefObject<HTMLDivElement | null> }) {
  const [svgMarkup, setSvgMarkup] = useState<string | null>(null);
  const close = useCallback(() => setSvgMarkup(null), []);

  const openFullscreen = useCallback(() => {
    // The rendered diagram is Mermaid's own <svg id="mermaid-…"> — scope to it so
    // we don't clone a toolbar/zoom-control icon (all 16×16 <svg>s in the block).
    const svg = blockRef.current?.querySelector('svg[id^="mermaid-"]');
    setSvgMarkup(svg ? svg.outerHTML : null);
  }, [blockRef]);

  useEffect(() => {
    if (svgMarkup == null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [svgMarkup, close]);

  const overlay =
    svgMarkup != null
      ? createPortal(
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-background/95 backdrop-blur-sm"
            // Close on a click that lands on the backdrop or the padding around
            // the diagram, but not one that bubbles up from the diagram itself —
            // hence the `target === currentTarget` gate on both this backdrop and
            // the inner wrapper (which must stay size-full so the SVG's own
            // width="100%" resolves against a real box rather than collapsing).
            onClick={(e) => {
              if (e.target === e.currentTarget) close();
            }}
            role="presentation"
          >
            <Button
              aria-label="Exit fullscreen"
              className="absolute top-4 right-4"
              onClick={close}
              size="icon-sm"
              title="Exit fullscreen"
              type="button"
              variant="ghost"
            >
              <CodeFullscreenIcon />
            </Button>
            <div
              className="flex size-full items-center justify-center p-8 [&>svg]:h-auto [&>svg]:max-h-full [&>svg]:w-auto [&>svg]:max-w-full"
              // The cloned SVG is Mermaid's own trusted, already-sanitized render.
              dangerouslySetInnerHTML={{ __html: svgMarkup }}
              onClick={(e) => {
                if (e.target === e.currentTarget) close();
              }}
              role="presentation"
            />
          </div>,
          getEmbedRoot() ?? document.body,
        )
      : null;

  return (
    <>
      <Button
        aria-label="View fullscreen"
        className={CODE_BLOCK_OVERLAY_BUTTON_CLASS}
        onClick={openFullscreen}
        size="icon-sm"
        title="View fullscreen"
        type="button"
        variant="ghost"
      >
        <CodeFullscreenIcon />
      </Button>
      {overlay}
    </>
  );
}

// A mermaid block: Streamdown's diagram (with its download/copy toolbar, its
// broken fullscreen disabled) plus our own fullscreen button in a matching pill.
// The `.chat-mermaid` rule nudges Streamdown's bar left so the two rows sit side
// by side — a continuous [download copy] [fullscreen] row at the top-right.
function ChatMermaidBlock({ block }: { block: ReactNode }) {
  const blockRef = useRef<HTMLDivElement>(null);
  return (
    <div ref={blockRef} className="chat-mermaid relative">
      {block}
      <div className="absolute top-2 right-2 z-20 flex items-center rounded-md border border-sidebar bg-sidebar/80 px-1.5 py-1 supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur">
        <MermaidFullscreen blockRef={blockRef} />
      </div>
    </div>
  );
}

function ChatCodeBlockPre({ children }: ComponentProps<"pre">) {
  const code = extractCodeText(children);
  const getCode = useCallback(() => code, [code]);
  // Soft-wrap long lines by default so users don't have to scroll horizontally
  // to read code blocks. The toggle restores Streamdown's native
  // horizontal-scroll view for when column alignment matters.
  const [wrap, setWrap] = useState(true);
  const toggleWrap = useCallback(() => setWrap((w) => !w), []);
  const block = isValidElement(children)
    ? cloneElement(children, { "data-block": "true" } as Record<string, unknown>)
    : children;

  // Mermaid renders its own toolbar (download/copy); we replace Streamdown's
  // broken fullscreen with our own button (portaled correctly), positioned as
  // the last item in that toolbar's row. Word-wrap/copy don't apply to a diagram.
  if (isMermaidPre(children)) {
    return <ChatMermaidBlock block={block} />;
  }

  return (
    <div className={cn("relative", wrap && "chat-code-wrap")}>
      {block}
      {/* Overlay actions, anchored left of Streamdown's own download button
          (which sits at the header's right edge). The py-1 padding plus a 1px
          transparent top/bottom border match the height of Streamdown's action
          pill (which has border + py-1) so our buttons land on the same line;
          the border is y-only so it doesn't add horizontal width that would
          push the row away from the pill. The -mr-1.5 pull tightens the gap so
          the row reads as one continuous set of controls. */}
      <div className="absolute top-2 right-12 z-10 -mr-1.5 flex items-center gap-0.5 border-y border-transparent py-1">
        <ChatCodeBlockWrapToggle onToggle={toggleWrap} wrap={wrap} />
        <ChatCodeBlockCopyButton getCode={getCode} />
      </div>
    </div>
  );
}

export const MessageResponse = memo(
  ({ className, components, controls, markFileLinks = false, ...props }: MessageResponseProps) => {
    const messageComponents = useMemo(
      () => ({ ...components, pre: ChatCodeBlockPre }),
      [components],
    );

    const messageControls = useMemo(() => getChatCodeControls(controls), [controls]);

    return (
      <MarkdownErrorBoundary source={props.children}>
        <Streamdown
          // wrap-anywhere is inherited, giving every prose descendant (including inline code) a break opportunity.
          className={cn(
            "size-full wrap-anywhere [&>*:first-child]:mt-0 [&>*:last-child]:mb-0",
            className,
          )}
          plugins={STREAMDOWN_PLUGINS}
          // Let links open on a plain click (and cmd/ctrl-click in a new tab)
          // instead of Streamdown's default "Open external link?" modal.
          linkSafety={CHAT_LINK_SAFETY}
          {...props}
          components={messageComponents}
          controls={messageControls}
          // Block remote image fetches that can exfiltrate data through URLs.
          rehypePlugins={
            markFileLinks ? FILE_LINK_STREAMDOWN_REHYPE_PLUGINS : SECURE_STREAMDOWN_REHYPE_PLUGINS
          }
        />
      </MarkdownErrorBoundary>
    );
  },
);

MessageResponse.displayName = "MessageResponse";

export type MessageToolbarProps = ComponentProps<"div">;

export const MessageToolbar = ({ className, children, ...props }: MessageToolbarProps) => (
  <div className={cn("mt-4 flex w-full items-center justify-between gap-4", className)} {...props}>
    {children}
  </div>
);
