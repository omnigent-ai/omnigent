// The Review menu's actions, defined once: the header's menu and ⌘K render these rows, so
// their labels, availability and behavior can't drift apart.

import {
  CheckCircle2Icon,
  ExternalLinkIcon,
  FileDiffIcon,
  FilePenLineIcon,
  MessageSquareTextIcon,
  RefreshCwIcon,
  Trash2Icon,
  type LucideIcon,
} from "lucide-react";

import { verdictUnavailableReason, type ReviewEvent } from "./reviewSubmit";
import {
  PENDING_CHANGE_UNRESOLVED,
  type ReviewOffer,
  type ReviewSubmitController,
} from "./useReviewSubmit";

export interface ReviewCommandRow {
  id: "submit" | "approve" | "request-changes" | "check" | "github" | "discard" | "keep-draft";
  paletteLabel: string;
  icon: LucideIcon;
  keywords: string[];
  /** Why it can't run now; a row with a reason shows greyed out. */
  disabledReason: string | null;
  run: () => void;
}

const KEYWORDS = ["review", "pull request", "github", "submit"];

/**
 * The rows for `review`: on this Mac, Submit Review…, Approve… and Request Changes…, each
 * opening the menu on its verdict, plus Check Again while a submit is unconfirmed; elsewhere,
 * Review on GitHub. None for a PR that can't be reviewed.
 */
export function reviewCommandRows(
  offer: ReviewOffer,
  review: ReviewSubmitController | null,
  {
    openMenu,
    openOnGitHub,
  }: { openMenu: (event: ReviewEvent | null) => void; openOnGitHub: (url: string) => void },
): ReviewCommandRow[] {
  if (offer.kind === "none") return [];
  if (offer.kind === "link") {
    return [
      {
        id: "github",
        paletteLabel: offer.label,
        icon: ExternalLinkIcon,
        keywords: [...KEYWORDS, "browser", "open"],
        disabledReason: null,
        run: () => openOnGitHub(offer.url),
      },
    ];
  }
  if (!review) return [];
  const { finish } = review;
  const unconfirmed = review.entry.status.kind === "uncertain";
  const held = unconfirmed
    ? "GitHub didn’t confirm your last review. Check whether it was submitted first."
    : finish?.unresolved
      ? PENDING_CHANGE_UNRESOLVED
      : null;
  const verdictRow = (
    id: "approve" | "request-changes",
    event: ReviewEvent,
    paletteLabel: string,
    icon: LucideIcon,
  ): ReviewCommandRow => ({
    id,
    paletteLabel,
    icon,
    keywords: [...KEYWORDS, id === "approve" ? "approve" : "request changes"],
    disabledReason: held ?? verdictUnavailableReason(event, review.viewer, review.author),
    run: () => openMenu(event),
  });
  const rows: ReviewCommandRow[] = [
    {
      id: "submit",
      paletteLabel: finish ? "Finish Review…" : "Submit Review…",
      icon: MessageSquareTextIcon,
      keywords: [...KEYWORDS, "comment"],
      disabledReason: held,
      run: () => openMenu(null),
    },
    // Verdicts the menu doesn't offer, as through an editor's host, aren't listed at all.
    ...(review.events.includes("APPROVE")
      ? [verdictRow("approve", "APPROVE", "Approve Pull Request…", CheckCircle2Icon)]
      : []),
    ...(review.events.includes("REQUEST_CHANGES")
      ? [verdictRow("request-changes", "REQUEST_CHANGES", "Request Changes…", FileDiffIcon)]
      : []),
  ];
  if (finish?.discardable) {
    rows.push({
      id: "discard",
      paletteLabel: "Discard Pending Review…",
      icon: Trash2Icon,
      keywords: [...KEYWORDS, "discard", "delete", "pending"],
      disabledReason: held ?? (finish.total === null ? "Discard it on GitHub." : null),
      run: () => {
        openMenu(null);
        finish.discard.ask();
      },
    });
  }
  if (finish?.unresolved?.kind === "discard") {
    rows.push({
      id: "check",
      paletteLabel: "Check Whether Your Review Was Discarded",
      icon: RefreshCwIcon,
      keywords: [...KEYWORDS, "check", "again", "discard"],
      disabledReason: finish.unresolved.hold.checking ? "Checking GitHub…" : null,
      run: review.checkAgain,
    });
  }
  if (unconfirmed) {
    rows.push({
      id: "check",
      paletteLabel: "Check Whether Your Review Was Submitted",
      icon: RefreshCwIcon,
      keywords: [...KEYWORDS, "check", "again", "unconfirmed"],
      disabledReason:
        review.entry.status.kind === "uncertain" && review.entry.status.checking
          ? "Checking GitHub…"
          : null,
      run: review.checkAgain,
    });
  }
  if (review.keepAsDraft.available) {
    rows.push({
      id: "keep-draft",
      paletteLabel: "Keep Your Unconfirmed Review as a Draft…",
      icon: FilePenLineIcon,
      keywords: [...KEYWORDS, "keep", "draft", "unconfirmed", "edit"],
      disabledReason: null,
      run: () => {
        openMenu(null);
        review.keepAsDraft.ask();
      },
    });
  }
  return rows;
}
