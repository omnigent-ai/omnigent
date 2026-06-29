"use client";

import { Button } from "@/components/ui/button";
import { ButtonGroup, ButtonGroupText } from "@/components/ui/button-group";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import type { UIMessage } from "ai";
import { CheckIcon, ChevronLeftIcon, ChevronRightIcon, CopyIcon, WrapTextIcon } from "lucide-react";
import type { ComponentProps, HTMLAttributes, ReactElement, ReactNode } from "react";
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
import { Streamdown, type StreamdownProps } from "streamdown";

import {
  CHAT_LINK_SAFETY,
  SECURE_STREAMDOWN_REHYPE_PLUGINS,
  STREAMDOWN_PLUGINS,
} from "./streamdown-security";

export type MessageProps = HTMLAttributes<HTMLDivElement> & {
  from: UIMessage["role"];
};

export const Message = ({ className, from, ...props }: MessageProps) => (
  <div
    className={cn(
      "group flex w-full max-w-[95%] flex-col gap-2",
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
      // 15px text / 24px line-height at the 16px desktop root, in rem so the
      // mobile root-font-size bump (index.css) still scales both. User and
      // assistant prose share this wrapper, so they stay in lockstep.
      "is-user:dark flex w-fit min-w-0 max-w-full flex-col gap-2 overflow-hidden text-[0.9375rem] leading-6 tracking-[-0.01em]",
      "group-[.is-user]:ml-auto group-[.is-user]:rounded-2xl group-[.is-user]:bg-muted group-[.is-user]:px-4 group-[.is-user]:py-3 group-[.is-user]:text-foreground group-[.is-user]:ring-1 group-[.is-user]:ring-border/60",
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
  <div className={cn("flex items-center gap-1", className)} {...props}>
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
  variant = "ghost",
  size = "icon-sm",
  ...props
}: MessageActionProps) => {
  const button = (
    <Button size={size} type="button" variant={variant} {...props}>
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

export type MessageResponseProps = Omit<StreamdownProps, "rehypePlugins">;

function getChatCodeControls(controls: StreamdownProps["controls"]): StreamdownProps["controls"] {
  if (typeof controls === "object" && controls !== null) {
    const codeControls = controls.code;
    return {
      ...controls,
      code: {
        ...(typeof codeControls === "object" && codeControls !== null ? codeControls : {}),
        copy: false,
        download: true,
      },
    };
  }

  return { code: { copy: false, download: true } };
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

// Shared visual style for the buttons overlaid on a chat code block (copy,
// wrap toggle). The frosted/ghost look matches the rest of the chat surface;
// positioning lives on the container in ChatCodeBlockPre, not here, so the
// buttons stay layout-agnostic.
const CODE_BLOCK_OVERLAY_BUTTON_CLASS =
  "size-8 bg-sidebar/80 text-muted-foreground hover:text-foreground supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur";

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

  const Icon = isCopied ? CheckIcon : CopyIcon;

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
      <Icon size={14} />
    </Button>
  );
}

function ChatCodeBlockWrapToggle({ wrap, onToggle }: { wrap: boolean; onToggle: () => void }) {
  return (
    <Button
      aria-label="Toggle word wrap"
      aria-pressed={wrap}
      // Brighten when active so the pressed state reads at a glance.
      className={cn(CODE_BLOCK_OVERLAY_BUTTON_CLASS, wrap && "text-foreground")}
      onClick={onToggle}
      size="icon-sm"
      title={wrap ? "Disable word wrap" : "Enable word wrap"}
      type="button"
      variant="ghost"
    >
      <WrapTextIcon size={14} />
    </Button>
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

  return (
    <div className={cn("relative", wrap && "chat-code-wrap")}>
      {block}
      {/* Overlay actions, anchored left of Streamdown's own download button
          (which sits at the header's right edge). A flex row lets the buttons
          self-arrange, so neither needs a hardcoded horizontal offset. */}
      <div className="absolute top-2 right-12 z-10 flex items-center gap-1">
        <ChatCodeBlockWrapToggle onToggle={toggleWrap} wrap={wrap} />
        <ChatCodeBlockCopyButton getCode={getCode} />
      </div>
    </div>
  );
}

export const MessageResponse = memo(
  ({ className, components, controls, ...props }: MessageResponseProps) => {
    const messageComponents = useMemo(
      () => ({ ...components, pre: ChatCodeBlockPre }),
      [components],
    );

    const messageControls = useMemo(() => getChatCodeControls(controls), [controls]);

    return (
      <Streamdown
        className={cn("size-full [&>*:first-child]:mt-0 [&>*:last-child]:mb-0", className)}
        plugins={STREAMDOWN_PLUGINS}
        // Let links open on a plain click (and cmd/ctrl-click in a new tab)
        // instead of Streamdown's default "Open external link?" modal.
        linkSafety={CHAT_LINK_SAFETY}
        {...props}
        components={messageComponents}
        controls={messageControls}
        // Block remote image fetches that can exfiltrate data through URLs.
        rehypePlugins={SECURE_STREAMDOWN_REHYPE_PLUGINS}
      />
    );
  },
  (prevProps, nextProps) =>
    prevProps.children === nextProps.children && nextProps.isAnimating === prevProps.isAnimating,
);

MessageResponse.displayName = "MessageResponse";

export type MessageToolbarProps = ComponentProps<"div">;

export const MessageToolbar = ({ className, children, ...props }: MessageToolbarProps) => (
  <div className={cn("mt-4 flex w-full items-center justify-between gap-4", className)} {...props}>
    {children}
  </div>
);
