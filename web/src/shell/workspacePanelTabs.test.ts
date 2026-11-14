import { describe, expect, it } from "vitest";
import { ARIA_MOD_KEY, MOD_KEY, SHIFT_KEY } from "@/components/KeyboardShortcut";
import { TAB_BADGE_BASE } from "./railTabs";
import {
  WORKSPACE_PANEL_TABS,
  type WorkspacePanelTab,
  type WorkspacePanelTabContext,
  workspacePanelTabPresentation,
} from "./workspacePanelTabs";

const READY: WorkspacePanelTabContext = {
  pending: false,
  showFilesPanel: true,
  showGithubTab: true,
  showContentSearch: true,
  changedCount: 0,
  agentCount: 0,
  subagentsWorking: 0,
  digit: undefined,
  digitAriaShortcut: undefined,
};

function shownTabs(context: WorkspacePanelTabContext): WorkspacePanelTab[] {
  return (Object.keys(WORKSPACE_PANEL_TABS) as WorkspacePanelTab[]).filter(
    (tab) => workspacePanelTabPresentation(WORKSPACE_PANEL_TABS[tab], context) !== null,
  );
}

describe("workspacePanelTabPresentation", () => {
  it("draws every tab once the session's panels are available", () => {
    expect(shownTabs(READY)).toEqual(["content-search", "files", "changes", "github", "subagents"]);
  });

  it("keeps Files, Changes and Pull Requests drawn but not Content search while pending", () => {
    const pending = { ...READY, pending: true, showFilesPanel: false, showGithubTab: false };
    expect(shownTabs(pending)).toEqual(["files", "changes", "github", "subagents"]);
  });

  it("hides file-backed tabs without a files panel, and Pull Requests without GitHub", () => {
    expect(shownTabs({ ...READY, showFilesPanel: false })).toEqual(["github", "subagents"]);
    expect(shownTabs({ ...READY, showGithubTab: false })).toEqual([
      "content-search",
      "files",
      "changes",
      "subagents",
    ]);
    expect(shownTabs({ ...READY, showContentSearch: false })).toEqual([
      "files",
      "changes",
      "github",
      "subagents",
    ]);
  });

  it("defaults the aria-label to the label and the shortcuts to the tab's digit", () => {
    const files = workspacePanelTabPresentation(WORKSPACE_PANEL_TABS.files, {
      ...READY,
      digit: "2",
      digitAriaShortcut: "Control+2",
    });
    expect(files).toMatchObject({
      label: "Files",
      ariaLabel: "Files",
      tooltipShortcut: "2",
      ariaKeyShortcuts: "Control+2",
      badge: null,
    });
    expect(workspacePanelTabPresentation(WORKSPACE_PANEL_TABS.github, READY)).toMatchObject({
      ariaLabel: "Pull Requests",
      tooltipShortcut: undefined,
      ariaKeyShortcuts: undefined,
    });
  });

  it("gives Content search its own chord, combined with its digit when it has one", () => {
    expect(
      workspacePanelTabPresentation(WORKSPACE_PANEL_TABS["content-search"], READY),
    ).toMatchObject({
      ariaLabel: "Content search",
      tooltipShortcut: [MOD_KEY, SHIFT_KEY, "F"],
      ariaKeyShortcuts: `${ARIA_MOD_KEY}+Shift+F`,
    });
    expect(
      workspacePanelTabPresentation(WORKSPACE_PANEL_TABS["content-search"], {
        ...READY,
        digit: "1",
        digitAriaShortcut: "Control+1",
      }),
    ).toMatchObject({
      tooltipShortcut: [MOD_KEY, SHIFT_KEY, "F"],
      ariaKeyShortcuts: `${ARIA_MOD_KEY}+Shift+F Control+1`,
    });
  });

  it("announces the Changes count only when files changed", () => {
    expect(workspacePanelTabPresentation(WORKSPACE_PANEL_TABS.changes, READY)).toMatchObject({
      ariaLabel: "Changes",
      badge: null,
    });
    expect(
      workspacePanelTabPresentation(WORKSPACE_PANEL_TABS.changes, { ...READY, changedCount: 3 }),
    ).toMatchObject({
      ariaLabel: "Changes 3 changed",
      badge: { text: 3, className: "sr-only" },
    });
  });

  it("badges Agents with the total, or working/total in the success tone", () => {
    expect(
      workspacePanelTabPresentation(WORKSPACE_PANEL_TABS.subagents, { ...READY, agentCount: 2 }),
    ).toMatchObject({
      ariaLabel: "Agents 2",
      badge: { text: 2, className: `${TAB_BADGE_BASE} sr-only text-muted-foreground` },
    });
    expect(
      workspacePanelTabPresentation(WORKSPACE_PANEL_TABS.subagents, {
        ...READY,
        agentCount: 3,
        subagentsWorking: 1,
      }),
    ).toMatchObject({
      ariaLabel: "Agents 1/3",
      badge: { text: "1/3", className: `${TAB_BADGE_BASE} sr-only text-success` },
    });
  });

  it("lets a composed descriptor suppress a default shortcut by returning undefined", () => {
    const quietFiles = { ...WORKSPACE_PANEL_TABS.files, tooltipShortcut: () => undefined };
    expect(
      workspacePanelTabPresentation(quietFiles, {
        ...READY,
        digit: "2",
        digitAriaShortcut: "Control+2",
      }),
    ).toMatchObject({ tooltipShortcut: undefined, ariaKeyShortcuts: "Control+2" });
  });
});
