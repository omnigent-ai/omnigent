// "What's New?" pill shown at the top of the app when bundled release-notes
// entries are newer than the last one this browser has seen. Clicking it opens
// a dialog with the unseen entries and marks them seen; × just dismisses.

import { SparklesIcon, XIcon } from "lucide-react";
import { useState } from "react";

import { MessageResponse } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
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

function formatEntryDate(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function EntryList({ entries }: { entries: WhatsNewEntry[] }) {
  return (
    <div className="flex flex-col gap-5">
      {entries.map((entry) => (
        <section key={entry.date} aria-label={entry.title} className="flex flex-col gap-1">
          <p className="text-xs text-muted-foreground">{formatEntryDate(entry.date)}</p>
          <h3 className="text-sm font-semibold">{entry.title}</h3>
          <div className="text-sm">
            <MessageResponse>{entry.body}</MessageResponse>
          </div>
        </section>
      ))}
    </div>
  );
}

export function WhatsNew({ entries = WHATS_NEW_ENTRIES }: { entries?: WhatsNewEntry[] }) {
  // Read once per page load: the set shown stays stable while the dialog is open.
  const [unseen] = useState(() => unseenEntries(entries, readLastSeen()));
  const [badgeVisible, setBadgeVisible] = useState(unseen.length > 0);
  const [open, setOpen] = useState(false);
  const [showEarlier, setShowEarlier] = useState(false);

  const markSeen = () => {
    if (entries.length > 0) writeLastSeen(entries[0].date);
    setBadgeVisible(false);
  };
  const earlier = entries.filter((entry) => !unseen.includes(entry));

  return (
    <>
      {badgeVisible && (
        <div
          role="status"
          className="fixed left-1/2 top-[calc(4.25rem+var(--omnigent-safe-top,0px))] sm:top-[calc(0.5rem+var(--omnigent-safe-top,0px))] z-50 flex -translate-x-1/2 items-center gap-1 rounded-full bg-success py-1 pr-1 pl-3 text-sm text-white shadow-md"
        >
          <button
            type="button"
            className="flex items-center gap-1.5 whitespace-nowrap rounded-full outline-none focus-visible:ring-2 focus-visible:ring-white/70"
            onClick={() => {
              markSeen();
              setOpen(true);
            }}
          >
            <SparklesIcon className="size-4" aria-hidden="true" />
            <span className="font-semibold">What&apos;s New?</span>
            {/* Narrow screens show just the label so the pill fits beside the header controls. */}
            <span className="sr-only sm:not-sr-only">
              See features released since your last visit.
            </span>
          </button>
          <Button
            variant="ghost"
            size="icon"
            aria-label="Dismiss What's New"
            className="size-6 rounded-full text-white hover:bg-white/20 hover:text-white"
            onClick={markSeen}
          >
            <XIcon className="size-3.5" />
          </Button>
        </div>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[80dvh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>What&apos;s New</DialogTitle>
            <DialogDescription>Features released since your last visit.</DialogDescription>
          </DialogHeader>
          <EntryList entries={unseen} />
          {earlier.length > 0 &&
            (showEarlier ? (
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
