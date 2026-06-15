"use client";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { ChevronRightIcon, SparklesIcon } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Streamdown } from "streamdown";

import { Shimmer } from "./shimmer";
import {
  CHAT_LINK_SAFETY,
  SECURE_STREAMDOWN_REHYPE_PLUGINS,
  STREAMDOWN_PLUGINS,
} from "./streamdown-security";

interface ReasoningContextValue {
  isStreaming: boolean;
  isOpen: boolean;
  setIsOpen: (open: boolean) => void;
  duration: number | undefined;
  /**
   * Whether this reasoning section can be expanded to reveal content.
   * `false` when there is nothing to show (a settled section with empty
   * text) — the trigger then renders as a flat, non-interactive header
   * with no chevron, and the content is not rendered at all.
   */
  expandable: boolean;
}

const ReasoningContext = createContext<ReasoningContextValue | null>(null);

export const useReasoning = () => {
  const context = useContext(ReasoningContext);
  if (!context) {
    throw new Error("Reasoning components must be used within Reasoning");
  }
  return context;
};

export type ReasoningProps = ComponentProps<typeof Collapsible> & {
  isStreaming?: boolean;
  duration?: number;
  /**
   * Whether the section has content worth expanding. Defaults to `true`
   * so existing callers keep the collapsible behavior. When `false`, the
   * collapsible is locked closed and the trigger renders as a flat,
   * non-interactive header (see `ReasoningTrigger` / `ReasoningContent`).
   */
  expandable?: boolean;
};

export const Reasoning = memo(
  ({
    className,
    isStreaming = false,
    duration,
    expandable = true,
    children,
    ...props
  }: ReasoningProps) => {
    // Open state follows isStreaming by default, but a manual user
    // toggle takes precedence until the next time streaming starts.
    // Resetting on the next stream means a new reasoning section
    // doesn't inherit the user's choice from the previous one.
    const [userOverride, setUserOverride] = useState<boolean | null>(null);
    // A non-expandable section is always closed — there's nothing inside.
    const isOpen = expandable ? (userOverride ?? isStreaming) : false;

    useEffect(() => {
      if (isStreaming) setUserOverride(null);
    }, [isStreaming]);

    const handleOpenChange = useCallback((newOpen: boolean) => {
      setUserOverride(newOpen);
    }, []);

    const contextValue = useMemo(
      () => ({ duration, isOpen, isStreaming, setIsOpen: handleOpenChange, expandable }),
      [duration, isOpen, isStreaming, handleOpenChange, expandable],
    );

    return (
      <ReasoningContext.Provider value={contextValue}>
        <Collapsible
          className={cn("not-prose", className)}
          // Radix locks interaction when disabled — keeps the header from
          // being clickable / focusable when there's nothing to reveal.
          disabled={!expandable}
          onOpenChange={handleOpenChange}
          open={isOpen}
          {...props}
        >
          {children}
        </Collapsible>
      </ReasoningContext.Provider>
    );
  },
);

export type ReasoningTriggerProps = ComponentProps<typeof CollapsibleTrigger> & {
  getThinkingMessage?: (isStreaming: boolean, duration?: number) => ReactNode;
};

const defaultGetThinkingMessage = (isStreaming: boolean, duration?: number) => {
  if (isStreaming) {
    // Glimmer the label while the thought is in flight, matching the
    // "Working…" status pill — the shimmer settles into static text once
    // the section stops streaming.
    return (
      <Shimmer as="span" duration={1.5}>
        Thinking...
      </Shimmer>
    );
  }
  if (duration === undefined) {
    return <span>Thought for a few seconds</span>;
  }
  return <span>Thought for {duration.toFixed(1)} seconds</span>;
};

export const ReasoningTrigger = memo(
  ({
    className,
    children,
    getThinkingMessage = defaultGetThinkingMessage,
    ...props
  }: ReasoningTriggerProps) => {
    const { isStreaming, isOpen, duration, expandable } = useReasoning();

    const label = (
      <>
        <SparklesIcon className="size-3.5 shrink-0" />
        <span className="min-w-0 flex-1 truncate">{getThinkingMessage(isStreaming, duration)}</span>
        {expandable && (
          <ChevronRightIcon
            className={cn(
              "size-3.5 shrink-0 transition-transform",
              isOpen ? "rotate-90" : "rotate-0",
            )}
          />
        )}
      </>
    );

    // Nothing to reveal: render a flat, non-interactive header (no
    // chevron, no button semantics, no hover/pointer affordance) rather
    // than a collapsible trigger that expands into emptiness.
    if (!expandable) {
      return (
        <div
          className={cn(
            "flex w-full items-center gap-1.5 py-0.5 text-left text-muted-foreground text-xs",
            className,
          )}
        >
          {children ?? label}
        </div>
      );
    }

    return (
      <CollapsibleTrigger
        className={cn(
          "flex w-full cursor-pointer items-center gap-1.5 py-0.5 text-left text-muted-foreground text-xs transition-colors hover:text-foreground",
          className,
        )}
        {...props}
      >
        {children ?? label}
      </CollapsibleTrigger>
    );
  },
);

export type ReasoningContentProps = ComponentProps<typeof CollapsibleContent> & {
  children: string;
};

export const ReasoningContent = memo(({ className, children, ...props }: ReasoningContentProps) => {
  const { expandable } = useReasoning();

  // Non-expandable section has no content to reveal — render nothing so
  // there's no empty collapsible region under the flat header.
  if (!expandable) return null;

  return (
    <CollapsibleContent
      className={cn(
        "mt-1 ml-2 border-l pl-3 py-1 text-xs",
        "data-[state=closed]:fade-out-0 data-[state=closed]:slide-out-to-top-2 data-[state=open]:slide-in-from-top-2 text-muted-foreground outline-none data-[state=closed]:animate-out data-[state=open]:animate-in",
        className,
      )}
      {...props}
    >
      <Streamdown
        plugins={STREAMDOWN_PLUGINS}
        // Let links open on a plain click (and cmd/ctrl-click in a new tab)
        // instead of Streamdown's default "Open external link?" modal.
        linkSafety={CHAT_LINK_SAFETY}
        // Block remote image fetches that can exfiltrate data through URLs.
        rehypePlugins={SECURE_STREAMDOWN_REHYPE_PLUGINS}
      >
        {children}
      </Streamdown>
    </CollapsibleContent>
  );
});

Reasoning.displayName = "Reasoning";
ReasoningTrigger.displayName = "ReasoningTrigger";
ReasoningContent.displayName = "ReasoningContent";
