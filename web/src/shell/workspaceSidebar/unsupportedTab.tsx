// A tab the sidebar can't show, such as one saved by a newer version of the app, shows
// as unsupported, with a way to close it.

import { FileIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

import type { TabTitle } from "../workspaceTabs/WorkspaceStripTab";

export const UNSUPPORTED_TAB_TITLE: TabTitle = {
  label: "Unsupported tab",
  tooltip: "This tab needs a newer version of Omnigent.",
  icon: FileIcon,
  connecting: false,
};

/** What an unsupported tab shows: why, and a way to close it. */
export function UnsupportedTabContent({ onClose }: { onClose: () => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
      <p className="text-sm text-muted-foreground">This tab needs a newer version of Omnigent.</p>
      <Button type="button" variant="outline" size="sm" onClick={onClose}>
        Close tab
      </Button>
    </div>
  );
}
