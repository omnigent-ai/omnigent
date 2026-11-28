// A pull request reference in rendered Markdown: `#123` or a link to a PR. Once this Mac has
// looked the PR up, it shows the PR's status as an icon and its number in the status's color,
// and its title and state in a hover card; until then, and wherever nothing can read it, it's a
// plain link. Copy and Find see only the reference as written.

import {
  type ComponentProps,
  type MouseEvent,
  type ReactElement,
  type ReactNode,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import type { Components } from "react-markdown";

import { GitPullRequestIcon } from "lucide-react";

import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { isBehindOpenLayer } from "@/lib/automaticFocus";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { useHasBeenNearViewport } from "@/lib/pullRequestReferences/nearViewport";
import { MENTION_TAG, PULL_REQUEST_REFERENCE_TAG } from "@/lib/pullRequestReferences/rehype";
import type { PullRequestReference } from "@/lib/pullRequestReferences/reference";
import { usePullRequestReferenceScope } from "@/lib/pullRequestReferences/scope";
import {
  type ReferencedPullRequest,
  useReferencedPullRequest,
} from "@/lib/pullRequestReferences/useReferencedPullRequest";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import { pullRequestLifecycleVisual } from "@/shell/PullRequestLifecycleBadge";

const asWritten = (trigger: ReactElement): ReactElement => trigger;

/** A link as Markdown draws it everywhere else, so a reference nothing can read looks the same. */
const LINK_CLASS = "wrap-anywhere font-medium text-primary underline";

/** How long a pointer rests on a reference before its card opens, and leaves before it closes. */
const CARD_OPEN_DELAY_MS = 350;
const CARD_CLOSE_DELAY_MS = 120;

/**
 * What a renderer adds to its PR references: its own menu around the link, what a click does,
 * and an action at the foot of the hover card. Without one, a reference is a plain link.
 */
export interface PullRequestReferenceLinkBehavior {
  wrapTrigger: (pullRequest: CodeHostPullRequest, trigger: ReactElement) => ReactElement;
  onClick: (pullRequest: CodeHostPullRequest, event: MouseEvent<HTMLAnchorElement>) => void;
  /** Null where it offers none, so a reference with no details either stays a plain link. */
  cardAction: (pullRequest: CodeHostPullRequest) => ReactNode;
}

/** The card's state, by what the read said: the PR's lifecycle, or why there's none. */
function cardState(lookup: ReferencedPullRequest): { label: string; className: string } | null {
  if (lookup.status === "loading") return { label: "Loading…", className: "text-muted-foreground" };
  if (lookup.status !== "found" || !lookup.pr.state) return null;
  const visual = pullRequestLifecycleVisual(lookup.pr.state, lookup.pr.is_draft);
  const known = LIFECYCLE_LABELS.has(visual.label);
  return {
    label: known ? visual.label : "State unknown",
    className: known ? visual.textClassName : "text-muted-foreground",
  };
}

const LIFECYCLE_LABELS = new Set(["Open", "Draft", "Merged", "Closed"]);

/** What the card says in the title's place where the read has no PR to show. */
const NO_PULL_REQUEST: Record<"missing" | "failed" | "unread", string> = {
  missing: "Not found, or you can’t see it.",
  failed: "Couldn’t load its details. Try again in a moment.",
  unread: "Its details aren’t available here.",
};

/**
 * A reference's hover card: where the PR lives and when it last changed on one quiet line, its
 * title, then its state and the renderer's action across the foot.
 */
function PullRequestReferenceCard({
  reference,
  lookup,
  action,
}: {
  reference: PullRequestReference;
  lookup: ReferencedPullRequest;
  action?: ReactNode;
}) {
  const pr = lookup.pr;
  const repository = pr?.repository ?? reference.target.repository;
  const number = pr?.number ?? reference.target.number;
  const updated = pr?.updated_at ? Date.parse(pr.updated_at) : Number.NaN;
  const visual = pr?.state ? pullRequestLifecycleVisual(pr.state, pr.is_draft) : null;
  const StateIcon = visual?.Icon ?? GitPullRequestIcon;
  const state = cardState(lookup);
  return (
    <div className="flex flex-col" data-testid="pull-request-reference-card">
      <div className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
        <StateIcon
          aria-hidden
          className={cn("size-3.5 shrink-0", visual?.textClassName ?? "text-muted-foreground")}
        />
        {repository && <span className="min-w-0 truncate">{repository}</span>}
        <span aria-hidden className="opacity-60">
          ·
        </span>
        <span className="shrink-0 tabular-nums">#{number}</span>
        {Number.isFinite(updated) && (
          <time
            className="ml-auto shrink-0 pl-2"
            dateTime={pr!.updated_at!}
            title={`Updated ${new Date(updated).toLocaleString()}`}
          >
            {relativeTime(updated)}
          </time>
        )}
      </div>
      {pr ? (
        <div
          className="mt-1.5 line-clamp-3 text-ui leading-snug font-semibold text-pretty text-popover-foreground"
          data-testid="pull-request-reference-card-title"
        >
          {pr.title || `Pull request #${number}`}
        </div>
      ) : lookup.status === "loading" ? (
        <div
          aria-hidden
          className="mt-2 flex flex-col gap-1.5"
          data-testid="pull-request-reference-card-loading"
        >
          <div className="h-3 w-11/12 animate-pulse rounded-sm bg-muted motion-reduce:animate-none" />
          <div className="h-3 w-3/5 animate-pulse rounded-sm bg-muted motion-reduce:animate-none" />
        </div>
      ) : (
        <div className="mt-1.5 text-ui text-muted-foreground">{NO_PULL_REQUEST[lookup.status]}</div>
      )}
      {(state || action) && (
        <div className="mt-2.5 flex min-h-6 items-center gap-2 border-t border-border pt-2">
          {state && (
            <span
              className={cn(
                "inline-flex items-center gap-1.5 text-xs font-medium",
                state.className,
              )}
              aria-label={`Pull request status: ${state.label}`}
              data-testid="pull-request-reference-card-state"
            >
              <span aria-hidden className="size-1.5 rounded-full bg-current" />
              {state.label}
            </span>
          )}
          {action && (
            <div className="ml-auto" data-pull-request-card-action="">
              {action}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** How a renderer draws a reference until it has a status, as it draws its other links. */
export interface PlainReferenceLook {
  className?: string;
  title?: string;
  /** Attributes the renderer's own links carry, such as the ones its styles select. */
  attributes?: Record<string, string>;
  /** What the plain link shows, where it differs from the reference's own text. */
  children?: ReactNode;
}

/**
 * A reference to `reference`'s PR, opening `href`. `children` is what the
 * author wrote, kept for a link with its own text; a bare reference or URL shows the reference's
 * label instead. `wrapTrigger` wraps the link in, say, the renderer's own menu, in either look.
 * `cardAction` shows at the foot of its hover card.
 */
export function PullRequestReferenceLink({
  reference,
  href,
  children,
  linkProps,
  plain,
  wrapTrigger = asWritten,
  cardAction,
}: {
  reference: PullRequestReference;
  href: string;
  children?: ReactNode;
  linkProps?: Omit<ComponentProps<"a">, "href" | "children">;
  plain?: PlainReferenceLook;
  wrapTrigger?: (trigger: ReactElement) => ReactElement;
  cardAction?: ReactNode;
}) {
  const scope = usePullRequestReferenceScope();
  const anchor = useRef<HTMLAnchorElement>(null);
  const near = useHasBeenNearViewport(anchor);
  const lookup = useReferencedPullRequest(reference.target, scope, near);
  const text = children ?? reference.label;
  const pr = lookup.pr?.state ? lookup.pr : null;
  // While nothing is known or offered, there's no card to open.
  const hasCard = pr !== null || Boolean(cardAction);
  const visual = pr ? pullRequestLifecycleVisual(pr.state!, pr.is_draft) : null;
  // A PR the read hasn't described keeps the renderer's plain look; its card still offers the action.
  const trigger = visual ? (
    <a
      ref={anchor}
      {...linkProps}
      className={cn(
        "wrap-anywhere font-medium no-underline hover:underline",
        visual.textClassName,
        linkProps?.className,
      )}
      data-pull-request-state={visual.label.toLowerCase()}
      aria-label={`${typeof text === "string" ? text : reference.label}, ${visual.label}: ${pr!.title}`}
      href={href}
      rel="noreferrer"
      target="_blank"
    >
      <visual.Icon
        aria-hidden
        className="mr-0.5 inline size-[0.95em] -translate-y-px align-middle select-none"
      />
      {text}
    </a>
  ) : (
    <a
      ref={anchor}
      {...linkProps}
      {...plain?.attributes}
      className={cn(plain?.className ?? LINK_CLASS, linkProps?.className)}
      title={plain?.title ?? linkProps?.title}
      href={href}
      rel="noreferrer"
      target="_blank"
    >
      {plain?.children ?? text}
    </a>
  );
  // One tree in every state, so the link stays the same element, and keeps focus, as its
  // status arrives.
  return (
    <ReferenceHoverCard anchor={anchor} wrapTrigger={wrapTrigger} trigger={trigger}>
      {hasCard ? (
        <PullRequestReferenceCard reference={reference} lookup={lookup} action={cardAction} />
      ) : null}
    </ReferenceHoverCard>
  );
}

/** Whether an open dialog or menu that doesn't hold the link covers it. */
function coveredByLayer(link: HTMLElement | null): boolean {
  return link !== null && isBehindOpenLayer(link);
}

/**
 * A reference's hover card, opened by pointer or focus. From the focused link, Tab moves into
 * the card's action, Shift+Tab goes back, Tab on goes past the link as if the card weren't
 * there, and Escape closes it on the link.
 */
function ReferenceHoverCard({
  anchor,
  wrapTrigger,
  trigger,
  children,
}: {
  anchor: RefObject<HTMLAnchorElement | null>;
  wrapTrigger: (trigger: ReactElement) => ReactElement;
  trigger: ReactElement;
  /** The card; null while there's none, which keeps it closed. */
  children: ReactNode | null;
}) {
  const [opened, setOpen] = useState(false);
  const hasCard = children !== null;
  const open = opened && hasCard;
  const content = useRef<HTMLDivElement>(null);
  // Set as Escape puts focus back on the link, whose focus would otherwise open it again.
  const dismissed = useRef(false);

  const action = () =>
    content.current?.querySelector<HTMLElement>(
      "[data-pull-request-card-action] button, [data-pull-request-card-action] a[href]",
    ) ?? null;
  const changeOpen = (next: boolean) => {
    // Focus in the card keeps it open, though the link it came from lost it.
    if (!next && content.current?.contains(document.activeElement)) return;
    if (next && (dismissed.current || !hasCard || coveredByLayer(anchor.current))) return;
    setOpen(next);
  };
  // A dialog or menu opening over the link, such as the PR sheet a ⌘-click opens, closes its card.
  useEffect(() => {
    if (!open) return;
    const observer = new MutationObserver(() => {
      if (coveredByLayer(anchor.current)) setOpen(false);
    });
    observer.observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["data-state"],
    });
    return () => observer.disconnect();
  }, [open, anchor]);
  const backToLink = () => {
    dismissed.current = true;
    anchor.current?.focus();
  };
  return (
    <HoverCard
      open={open}
      onOpenChange={changeOpen}
      openDelay={CARD_OPEN_DELAY_MS}
      closeDelay={CARD_CLOSE_DELAY_MS}
    >
      {wrapTrigger(
        <HoverCardTrigger
          asChild
          onKeyDown={(event) => {
            const target = action();
            if (event.key !== "Tab" || event.shiftKey || !open || !target) return;
            event.preventDefault();
            target.focus();
          }}
          onPointerEnter={() => {
            dismissed.current = false;
          }}
          onBlur={() => {
            dismissed.current = false;
          }}
        >
          {trigger}
        </HoverCardTrigger>,
      )}
      <HoverCardContent
        align="start"
        className="w-80 px-3.5 pt-3 pb-2"
        onEscapeKeyDown={() => {
          if (content.current?.contains(document.activeElement)) backToLink();
        }}
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          anchor.current?.focus();
          // Shift+Tab lands on the link, the card still open; Tab leaves from it, so the page's
          // order goes on past the link.
          if (event.shiftKey) event.preventDefault();
          else setOpen(false);
        }}
        onBlur={(event) => {
          const next = event.relatedTarget as Node | null;
          if (next !== anchor.current && !content.current?.contains(next)) setOpen(false);
        }}
      >
        <div ref={content}>{children}</div>
      </HoverCardContent>
    </HoverCard>
  );
}

/** Whether a link's text is its own URL, as GitHub writes a bare link it linked itself. */
function isAutolink(href: string, children: ReactNode): boolean {
  return typeof children === "string" && children === href;
}

/** A Markdown link: a PR reference when it opens a PR on a page that reads them, else plain. */
function PullRequestMarkdownLink({
  node: _node,
  href,
  children,
  ...props
}: ComponentProps<"a"> & { node?: unknown }) {
  const scope = usePullRequestReferenceScope();
  const resolved = scope && href ? scope.link(href) : null;
  if (!resolved || !href) {
    return (
      <a
        {...props}
        className={cn(LINK_CLASS, props.className)}
        href={href}
        rel="noreferrer"
        target="_blank"
      >
        {children}
      </a>
    );
  }
  return (
    <PullRequestReferenceLink reference={resolved.reference} href={href} linkProps={props}>
      {isAutolink(href, children) ? undefined : children}
    </PullRequestReferenceLink>
  );
}

/**
 * A `#123` written in prose: a reference in a view that reads them, else the text as written.
 * `behavior` gives it its renderer's menu, click and card.
 */
export function PullRequestShorthand({
  children,
  behavior,
}: {
  children?: ReactNode;
  behavior?: PullRequestReferenceLinkBehavior;
}): ReactNode {
  const scope = usePullRequestReferenceScope();
  const written = typeof children === "string" ? children : null;
  const resolved = scope && written ? scope.shorthand(written) : null;
  if (!resolved) return children;
  // The PR as the scope recognized it, with the hosts it knows, never parsed again.
  const pullRequest = resolved.reference.target;
  return (
    <PullRequestReferenceLink
      reference={resolved.reference}
      href={pullRequest.url}
      linkProps={behavior && { onClick: (event) => behavior.onClick(pullRequest, event) }}
      wrapTrigger={behavior && ((trigger) => behavior.wrapTrigger(pullRequest, trigger))}
      cardAction={behavior?.cardAction(pullRequest)}
    >
      {written}
    </PullRequestReferenceLink>
  );
}

/** An `@login` or `@org/team` written in prose: a link to it where the view's host is known. */
export function MentionLink({ children }: { children?: ReactNode }): ReactNode {
  const scope = usePullRequestReferenceScope();
  const written = typeof children === "string" ? children : null;
  const href = written && scope?.mention ? scope.mention(written.slice(1)) : null;
  if (!href) return children;
  return (
    <a className={LINK_CLASS} href={href} rel="noreferrer" target="_blank">
      {written}
    </a>
  );
}

/** The Markdown components that draw PR references and mentions; spread into a renderer's. */
export const PULL_REQUEST_REFERENCE_COMPONENTS = {
  a: PullRequestMarkdownLink,
  [PULL_REQUEST_REFERENCE_TAG]: PullRequestShorthand,
  [MENTION_TAG]: MentionLink,
} as Components;
