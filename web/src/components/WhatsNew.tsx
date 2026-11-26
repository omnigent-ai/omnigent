// "What's new" release notes. While bundled entries are newer than the last one
// this browser has seen, desktop shows a card under the sidebar's main
// navigation and phones show a pill. Clicking either opens the notes and marks
// them seen; × just dismisses. The profile menu reopens the notes any time.

import { SparklesIcon, XIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { create } from "zustand";

import { MessageResponse } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  readLastSeen,
  unseenEntries,
  WHATS_NEW_ENTRIES,
  type WhatsNewEntry,
  writeLastSeen,
} from "@/whatsNew/whatsNew";

interface WhatsNewState {
  entries: WhatsNewEntry[];
  /** Read once per page load, so the set shown stays stable while the notes are open. */
  unseen: WhatsNewEntry[];
  noticeVisible: boolean;
  notesOpen: boolean;
  /** Set while the app shell mounts the notes (not in embedded views). */
  mounted: boolean;
}

function initialState(entries: WhatsNewEntry[]) {
  const unseen = unseenEntries(entries, readLastSeen());
  return { entries, unseen, noticeVisible: unseen.length > 0, notesOpen: false };
}

export const useWhatsNewStore = create<WhatsNewState>(() => ({
  ...initialState(WHATS_NEW_ENTRIES),
  mounted: false,
}));

/** Re-read the entries and the seen marker, as on a fresh page load. */
export function loadWhatsNew(entries: WhatsNewEntry[] = WHATS_NEW_ENTRIES) {
  useWhatsNewStore.setState(initialState(entries));
}

function dismissWhatsNew() {
  const { entries } = useWhatsNewStore.getState();
  if (entries.length > 0) writeLastSeen(entries[0].date);
  useWhatsNewStore.setState({ noticeVisible: false });
}

export function openWhatsNew() {
  dismissWhatsNew();
  useWhatsNewStore.setState({ notesOpen: true });
}

/** Whether the app shows release notes here, for the profile menu's entry. */
export function useWhatsNewAvailable() {
  return useWhatsNewStore((state) => state.mounted && state.entries.length > 0);
}

function formatEntryDate(date: string, options: Intl.DateTimeFormatOptions): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, options);
}

function EntryList({ entries }: { entries: WhatsNewEntry[] }) {
  return (
    <div className="flex flex-col gap-5">
      {entries.map((entry) => (
        <section key={entry.date} aria-label={entry.title} className="flex flex-col gap-1">
          <p className="text-xs text-muted-foreground">
            {formatEntryDate(entry.date, {
              weekday: "short",
              month: "short",
              day: "numeric",
              year: "numeric",
            })}
          </p>
          <h3 className="text-sm font-semibold">{entry.title}</h3>
          <div className="text-sm">
            <MessageResponse>{entry.body}</MessageResponse>
          </div>
        </section>
      ))}
    </div>
  );
}

function DismissButton({ className }: { className?: string }) {
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label="Dismiss What's new"
      className={cn("shrink-0 rounded-full text-muted-foreground", className)}
      onClick={dismissWhatsNew}
    >
      <XIcon className="size-3.5" />
    </Button>
  );
}

/** Desktop: the newest update's headline, under the sidebar's main navigation. */
export function WhatsNewSidebarCard() {
  const visible = useWhatsNewStore((state) => state.mounted && state.noticeVisible);
  const latest = useWhatsNewStore((state) => state.unseen[0]);
  if (!visible || !latest) return null;
  return (
    <div className="relative px-2 pt-2 max-md:hidden" role="status" data-testid="whats-new-card">
      <button
        type="button"
        className="flex w-full items-start gap-2.5 rounded-lg border bg-card py-2.5 pr-8 pl-2.5 text-left shadow-xs transition-colors outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
        onClick={openWhatsNew}
      >
        <SparklesIcon className="mt-px size-4 shrink-0 text-brand-accent" aria-hidden="true" />
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className="text-xs text-muted-foreground">
            What&apos;s new · {formatEntryDate(latest.date, { month: "short", day: "numeric" })}
          </span>
          <span className="line-clamp-2 text-sm leading-snug font-medium">{latest.title}</span>
        </span>
      </button>
      <DismissButton className="absolute top-3.5 right-3.5 size-6" />
    </div>
  );
}

/** Where the phone pill sits: under the header, above a chat composer, or at the screen's foot. */
export type WhatsNewPhonePlacement = "top" | "above-composer" | "bottom";

const PHONE_PLACEMENT_CLASS: Record<WhatsNewPhonePlacement, string> = {
  top: "top-[calc(4.25rem+var(--omnigent-safe-top,0px))]",
  "above-composer": "bottom-[calc(6.5rem+var(--omnigent-safe-bottom,0px))]",
  bottom: "bottom-[calc(1rem+var(--omnigent-safe-bottom,0px))]",
};

export function WhatsNew({ phonePlacement = "top" }: { phonePlacement?: WhatsNewPhonePlacement }) {
  const unseen = useWhatsNewStore((state) => state.unseen);
  const entries = useWhatsNewStore((state) => state.entries);
  const noticeVisible = useWhatsNewStore((state) => state.noticeVisible);
  const notesOpen = useWhatsNewStore((state) => state.notesOpen);
  const [showEarlier, setShowEarlier] = useState(false);
  useEffect(() => {
    useWhatsNewStore.setState({ mounted: true });
    return () => useWhatsNewStore.setState({ mounted: false });
  }, []);
  const earlier = entries.filter((entry) => !unseen.includes(entry));

  return (
    <>
      {noticeVisible && (
        <div
          role="status"
          // Phones only (desktop shows the sidebar card): a one-line pill placed so it
          // doesn't cover the page's own toolbar or content.
          data-testid="whats-new"
          className={cn(
            "fixed left-1/2 z-50 flex -translate-x-1/2 items-center gap-0.5 rounded-full border bg-card py-0.5 pr-0.5 pl-3 text-sm text-foreground shadow-md md:hidden",
            PHONE_PLACEMENT_CLASS[phonePlacement],
          )}
        >
          <button
            type="button"
            className="flex items-center gap-1.5 rounded-full py-1.5 whitespace-nowrap outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={openWhatsNew}
          >
            <SparklesIcon className="size-4 shrink-0 text-brand-accent" aria-hidden="true" />
            <span className="font-medium">What&apos;s new</span>
          </button>
          <DismissButton className="size-8" />
        </div>
      )}
      <Dialog
        open={notesOpen}
        onOpenChange={(open) => useWhatsNewStore.setState({ notesOpen: open })}
      >
        <DialogContent className="max-h-[80dvh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>What&apos;s new</DialogTitle>
            <DialogDescription>
              {unseen.length > 0 ? "Features released since your last visit." : "Recent updates."}
            </DialogDescription>
          </DialogHeader>
          {unseen.length > 0 && <EntryList entries={unseen} />}
          {earlier.length > 0 &&
            (showEarlier || unseen.length === 0 ? (
              <EntryList entries={earlier} />
            ) : (
              <Button
                variant="link"
                className="self-start px-0"
                onClick={() => setShowEarlier(true)}
              >
                Show earlier updates
              </Button>
            ))}
        </DialogContent>
      </Dialog>
    </>
  );
}
