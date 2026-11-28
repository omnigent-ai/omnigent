// The button that shows or hides the session's pull requests beside one of them, as the
// Files tab's button does its tree.

import { useId, useMemo } from "react";
import { PanelLeftCloseIcon, PanelLeftOpenIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

/** A pull request's tab that can show the list beside it, and whether it does now. */
export interface PullRequestListToggleState {
  shown: boolean;
  onToggle: () => void;
}

export const SHOW_PULL_REQUEST_LIST_LABEL = "Show the pull request list";
export const HIDE_PULL_REQUEST_LIST_LABEL = "Hide the pull request list";

export function PullRequestListToggle({ toggle }: { toggle: PullRequestListToggleState }) {
  const label = toggle.shown ? HIDE_PULL_REQUEST_LIST_LABEL : SHOW_PULL_REQUEST_LIST_LABEL;
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      aria-label={label}
      title={label}
      aria-pressed={toggle.shown}
      onClick={toggle.onToggle}
      className="shrink-0 text-muted-foreground"
    >
      {toggle.shown ? <PanelLeftCloseIcon /> : <PanelLeftOpenIcon />}
    </Button>
  );
}

/** ⌘K's way to the same toggle, while a tab on screen offers it. */
export function usePullRequestListToggleCommand(toggle: PullRequestListToggleState | undefined) {
  const instance = useId();
  const shown = toggle?.shown;
  const onToggle = toggle?.onToggle;
  const commands = useMemo<PaletteCommand[]>(
    () =>
      onToggle === undefined
        ? []
        : [
            {
              id: "toggle-pull-request-list",
              label: shown ? HIDE_PULL_REQUEST_LIST_LABEL : SHOW_PULL_REQUEST_LIST_LABEL,
              icon: shown ? PanelLeftCloseIcon : PanelLeftOpenIcon,
              keywords: ["pull requests", "pr", "sidebar", "list"],
              // Hiding the list takes the focused row away, so the tab places focus after it.
              movesFocus: true,
              run: onToggle,
            },
          ],
    [onToggle, shown],
  );
  usePaletteCommands(`pull-request-list-toggle:${instance}`, "Pull requests", commands);
}
