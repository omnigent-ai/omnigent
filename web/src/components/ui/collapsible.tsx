import * as CollapsiblePrimitive from "radix-ui/collapsible";
import { useLayoutEffect, useReducer, useRef, useState } from "react";

function Collapsible({ ...props }: React.ComponentProps<typeof CollapsiblePrimitive.Root>) {
  return <CollapsiblePrimitive.Root data-slot="collapsible" {...props} />;
}

function CollapsibleTrigger({
  ...props
}: React.ComponentProps<typeof CollapsiblePrimitive.CollapsibleTrigger>) {
  return <CollapsiblePrimitive.CollapsibleTrigger data-slot="collapsible-trigger" {...props} />;
}

function CollapsibleContent({
  ...props
}: React.ComponentProps<typeof CollapsiblePrimitive.CollapsibleContent>) {
  return <CollapsiblePrimitive.CollapsibleContent data-slot="collapsible-content" {...props} />;
}

const CONCEALED_CONTENT_STYLE: React.CSSProperties = {
  height: 0,
  marginBlock: 0,
  paddingBlock: 0,
  borderBlockWidth: 0,
  // Hidden children must not widen a shrink-to-fit ancestor such as a short reply.
  contain: "inline-size",
  overflow: "visible",
  visibility: "hidden",
  pointerEvents: "none",
  // Keeps the overflowing children out of the scrollable area while they still
  // intersect the viewport for IntersectionObserver-deferred renders.
  transform: "scale(0.01)",
  transformOrigin: "top left",
};

/**
 * Collapsible content whose children stay mounted while closed. Once the close
 * animation ends they stay laid out but unseen and take no space, so lazily
 * rendered markdown (Mermaid diagrams, highlighted code) is ready on reopen.
 */
function RetainedCollapsibleContent({
  open,
  style,
  ...props
}: Omit<
  React.ComponentProps<typeof CollapsiblePrimitive.CollapsibleContent>,
  "forceMount" | "ref"
> & {
  open: boolean;
}) {
  const contentRef = useRef<HTMLDivElement>(null);
  const [concealed, setConcealed] = useState(!open);
  const [, rerender] = useReducer((renders: number) => renders + 1, 0);
  useLayoutEffect(() => {
    // Radix measures opened content without re-rendering under forceMount, so
    // re-render on every open to refresh its height variable.
    if (open) {
      setConcealed(false);
      rerender();
      return;
    }
    const running = contentRef.current?.getAnimations?.() ?? [];
    if (running.length === 0) {
      setConcealed(true);
      return;
    }
    let cancelled = false;
    void Promise.allSettled(running.map((animation) => animation.finished)).then(() => {
      if (!cancelled) setConcealed(true);
    });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Holds the close animation's last frame only until concealed: a filled
  // animation frame would override the concealed inline styles.
  const closedStyle: React.CSSProperties = concealed
    ? { ...style, ...CONCEALED_CONTENT_STYLE }
    : { ...style, animationFillMode: "forwards" };

  return (
    <CollapsiblePrimitive.CollapsibleContent
      data-slot="collapsible-content"
      {...props}
      ref={contentRef}
      forceMount
      style={open ? style : closedStyle}
    />
  );
}

export { Collapsible, CollapsibleTrigger, CollapsibleContent, RetainedCollapsibleContent };
