import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation, useMoveToProject } from "@/hooks/useConversations";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, SHARING_OFF_REASON } from "@/lib/capabilities";
import { clearSessionMuteState, MUTED_LABEL_KEY, setMutedProjects } from "@/lib/sessionMute";
import { PinCapacityContext } from "@/lib/sidebarConfig";
import { isCommandListed } from "@/store/paletteCommands";

import { ConversationMenuItems } from "../Sidebar";
import {
  OWNER_ONLY_REASONS,
  PIN_CAPACITY_REASON,
  PIN_SAVING_REASON,
  type SessionCommandFacts,
  sessionCommand,
  sessionCommands,
  sessionOwnership,
  UNKNOWN_OWNER_REASON,
} from "./model";

// The row's Fork reads its restriction from this hook; each case sets it from its facts.
const forkRestriction = vi.hoisted(() => ({ reason: undefined as string | undefined }));
vi.mock("@/hooks/useSessionActionRestrictions", () => ({
  useSessionActionRestrictions: () => ({ forkDisabledReason: forkRestriction.reason }),
}));
// Whether a new window can open, as each case's facts say; the row reads it from this hook.
const sessionWindows = vi.hoisted(() => ({ open: null as ((sessionId: string) => void) | null }));
vi.mock("@/hooks/useOpenSessionWindow", () => ({
  useOpenSessionWindow: () => sessionWindows.open,
}));

const FACTS: SessionCommandFacts = {
  provisional: false,
  owner: true,
  ownerUnknown: false,
  archived: false,
  pinned: false,
  indentedSibling: false,
  atPinCapacity: false,
  pinSaving: false,
  unread: false,
  muted: false,
  mutedByProject: false,
  canCreateAutomation: true,
  currentProject: null,
  nestable: false,
  stoppable: true,
  canOpenWindow: false,
  server: { singleUser: false, sharingOff: false, siblingSessions: true, sessionMute: true },
};

const listed = (facts: SessionCommandFacts) =>
  sessionCommands(facts)
    .filter((command) => isCommandListed(command.availability))
    .map((command) => command.label);

const reasonOf = (facts: SessionCommandFacts, id: Parameters<typeof sessionCommand>[1]) => {
  const { availability } = sessionCommand(facts, id);
  return availability.available ? undefined : availability.reason;
};

describe("sessionCommands", () => {
  it("offers nothing while the session is still being created, as its row offers no menu", () => {
    expect(listed({ ...FACTS, provisional: true })).toEqual([]);
  });

  it("lists the owner's actions in the row menu's order", () => {
    expect(listed(FACTS)).toEqual([
      "Add nested agent",
      "Pin",
      "Share",
      "Fork",
      "Rename",
      "Mute",
      "Mark as unread",
      "Create automation",
      "Add to project",
      "Stop session",
      "Archive",
      "Delete",
    ]);
  });

  it("greys the owner's actions for someone it's shared with, who leaves instead of deleting", () => {
    const shared = { ...FACTS, owner: false, canCreateAutomation: false };
    expect(listed(shared)).toEqual([
      "Pin",
      "Share",
      "Fork",
      "Rename",
      "Mute",
      "Mark as unread",
      "Stop session",
      "Archive",
      "Leave session",
    ]);
    expect(reasonOf(shared, "share")).toBe(OWNER_ONLY_REASONS.share);
    expect(reasonOf(shared, "rename")).toBe(OWNER_ONLY_REASONS.rename);
    expect(reasonOf(shared, "stop")).toBe(OWNER_ONLY_REASONS.stop);
    expect(reasonOf(shared, "archive")).toBe(OWNER_ONLY_REASONS.archive);
  });

  it("greys the owner's actions, Delete and Leave while it can't tell whose the session is", () => {
    const unknown = { ...FACTS, owner: false, ownerUnknown: true, canCreateAutomation: false };
    expect(listed(unknown)).toEqual([
      "Pin",
      "Share",
      "Fork",
      "Rename",
      "Mute",
      "Mark as unread",
      "Stop session",
      "Archive",
      "Delete",
      "Leave session",
    ]);
    for (const id of ["share", "rename", "stop", "archive", "delete", "leave"] as const) {
      expect(reasonOf(unknown, id)).toBe(UNKNOWN_OWNER_REASON);
    }
  });

  it("offers Unarchive on an archived session, and nothing to pin or nest", () => {
    const archived = { ...FACTS, archived: true };
    const labels = listed(archived);
    expect(labels).toContain("Unarchive");
    expect(labels).not.toContain("Archive");
    expect(labels).not.toContain("Pin");
    expect(labels).not.toContain("Add nested agent");
  });

  it("says why Pin can't run while a pin saves or no slot is left", () => {
    expect(reasonOf({ ...FACTS, pinSaving: true }, "pin")).toBe(PIN_SAVING_REASON);
    expect(reasonOf({ ...FACTS, atPinCapacity: true }, "pin")).toBe(PIN_CAPACITY_REASON);
    // Unpinning frees a slot, so a full set of pins still allows it.
    expect(sessionCommand({ ...FACTS, pinned: true, atPinCapacity: true }, "unpin")).toMatchObject({
      availability: { available: true },
    });
  });

  it("lets a muted session be marked read but not flagged unread", () => {
    expect(listed({ ...FACTS, muted: true })).toEqual(expect.arrayContaining(["Unmute"]));
    expect(listed({ ...FACTS, muted: true })).not.toContain("Mark as unread");
    expect(listed({ ...FACTS, muted: true, unread: true })).toContain("Mark as read");
  });

  it("offers Open in new window after Add nested agent where a window can show the session", () => {
    expect(listed({ ...FACTS, canOpenWindow: true }).slice(0, 3)).toEqual([
      "Add nested agent",
      "Open in new window",
      "Pin",
    ]);
    expect(listed(FACTS)).not.toContain("Open in new window");
    expect(listed({ ...FACTS, canOpenWindow: true, provisional: true })).toEqual([]);
  });

  it("names the move after the project the session is filed in", () => {
    expect(sessionCommand({ ...FACTS, currentProject: "Payments" }, "move-to-project").label).toBe(
      "Move session",
    );
  });

  it("puts sharing turned off ahead of the viewer's own reason", () => {
    expect(
      reasonOf({ ...FACTS, owner: false, server: { ...FACTS.server, sharingOff: true } }, "share"),
    ).toBe(SHARING_OFF_REASON);
  });

  it("leaves Share and Leave out with one user, who only deletes", () => {
    const solo = { ...FACTS, owner: false, server: { ...FACTS.server, singleUser: true } };
    expect(listed(solo)).not.toContain("Share");
    expect(listed(solo)).not.toContain("Leave session");
    expect(listed(solo)).toContain("Delete");
  });

  it("offers Move to nest on the owner's top-level session where the server moves nests", () => {
    const nestable = { ...FACTS, nestable: true };
    expect(listed(nestable)).toContain("Move to nest");
    expect(listed(nestable).indexOf("Move to nest")).toBe(
      listed(nestable).indexOf("Add to project") + 1,
    );
    expect(sessionCommand(nestable, "move-to-nest").opensDialog).toBe(true);
    // Moving is the owner's, as the row menu offers it.
    expect(listed({ ...nestable, owner: false })).not.toContain("Move to nest");
    expect(listed(FACTS)).not.toContain("Move to nest");
  });

  it("marks only Stop, Delete and Leave as destructive, and the dialogs as moving focus", () => {
    const commands = sessionCommands(FACTS);
    expect(commands.filter((command) => command.destructive).map((command) => command.id)).toEqual([
      "stop",
      "delete",
      "leave",
    ]);
    expect(sessionCommand(FACTS, "pin").opensDialog).toBe(false);
    expect(sessionCommand(FACTS, "archive").opensDialog).toBe(true);
  });
});

const conversation: Conversation = {
  id: "conv_row",
  object: "conversation",
  title: "Row session",
  created_at: 0,
  updated_at: 0,
  labels: {},
  permission_level: null,
};

/** The row menu's items for `facts`: their names, and which of them are greyed. */
function rowMenu(facts: SessionCommandFacts) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  onTestFinished(() => queryClient.clear());
  const labels: Record<string, string> = facts.muted ? { [MUTED_LABEL_KEY]: "1" } : {};
  const row: Conversation = { ...conversation, labels };
  if (facts.mutedByProject) {
    setMutedProjects([{ id: "proj_muted", name: "Muted project", muted: true }], null);
    row.project_id = "proj_muted";
  }
  queryClient.setQueryData(["conversation-backfill", row.id], row);
  forkRestriction.reason = facts.forkDisabledReason;
  sessionWindows.open = facts.canOpenWindow ? vi.fn() : null;
  const noop = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <CapabilitiesProvider
        info={{
          ...FALLBACK_SERVER_INFO,
          session_mute: facts.server.sessionMute,
          sibling_sessions: facts.server.siblingSessions,
        }}
      >
        <PinCapacityContext.Provider value={facts.atPinCapacity}>
          <MemoryRouter>
            <TooltipProvider>
              <DropdownMenu open>
                <DropdownMenuTrigger>Actions</DropdownMenuTrigger>
                <DropdownMenuContent>
                  <ConversationMenuItems
                    conversation={row}
                    isPinned={facts.pinned}
                    isArchived={facts.archived}
                    isOwner={facts.owner}
                    sharingOff={facts.server.sharingOff}
                    isSingleUser={facts.server.singleUser}
                    canStop={facts.stoppable}
                    canMarkUnread={!facts.unread}
                    currentProject={facts.currentProject}
                    onTogglePinned={noop}
                    onMarkUnread={noop}
                    onMarkRead={noop}
                    moveToProject={{} as ReturnType<typeof useMoveToProject>}
                    setShareOpen={noop}
                    setForkOpen={noop}
                    canCreateAutomation={facts.canCreateAutomation}
                    setAutomationOpen={noop}
                    setIsEditing={noop}
                    setStopOpen={noop}
                    setDeleteOpen={noop}
                    setLeaveOpen={noop}
                    setMenuOpen={noop}
                    runArchive={noop}
                  />
                </DropdownMenuContent>
              </DropdownMenu>
            </TooltipProvider>
          </MemoryRouter>
        </PinCapacityContext.Provider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  const items = screen.getAllByRole("menuitem");
  const name = (item: HTMLElement) => item.textContent?.trim() ?? "";
  return {
    names: items.map(name),
    // Plain disabled items set data-disabled; ones that explain why set aria-disabled.
    greyed: items
      .filter(
        (item) =>
          item.hasAttribute("data-disabled") || item.getAttribute("aria-disabled") === "true",
      )
      .map(name),
  };
}

describe("sessionOwnership", () => {
  it("trusts a named owner or an owner grant, and fails closed on a row that has neither", () => {
    const row = (owner: string | null | undefined, level: number | null) => ({
      owner,
      permission_level: level,
    });
    expect(sessionOwnership(row("viewer", null), "viewer", false)).toBe("mine");
    expect(sessionOwnership(row("someone-else", 4), "viewer", false)).toBe("theirs");
    expect(sessionOwnership(row(undefined, 4), "viewer", false)).toBe("mine");
    expect(sessionOwnership(row(null, 1), "viewer", false)).toBe("unknown");
    expect(sessionOwnership(row(undefined, null), "viewer", false)).toBe("unknown");
    // The single-user runtime owns every session.
    expect(sessionOwnership(row(undefined, null), "local", true)).toBe("mine");
    expect(sessionOwnership(row(null, null), null, true)).toBe("mine");
  });

  it("fails closed on an ownerless row while the viewer is unknown on a multi-user server", () => {
    const ownerless = { owner: null, permission_level: null };
    expect(sessionOwnership(ownerless, null, false)).toBe("unknown");
    expect(sessionOwnership({ owner: undefined, permission_level: 1 }, null, false)).toBe(
      "unknown",
    );
    // Positive evidence still counts without a viewer: an owner grant on the row.
    expect(sessionOwnership({ owner: undefined, permission_level: 4 }, null, false)).toBe("mine");
  });
});

describe("sessionCommands against the sidebar row's menu", () => {
  afterEach(() => {
    cleanup();
    clearSessionMuteState();
  });

  it.each([
    ["the owner's session", FACTS],
    ["a session shared with the viewer", { ...FACTS, owner: false, canCreateAutomation: false }],
    ["an archived session", { ...FACTS, archived: true }],
    ["a session that can't be forked", { ...FACTS, forkDisabledReason: "Fork is off." }],
    ["a pinned, unread session", { ...FACTS, pinned: true, unread: true }],
    ["a full set of pins", { ...FACTS, atPinCapacity: true }],
    ["a session a new window can show", { ...FACTS, canOpenWindow: true }],
    ["a muted session", { ...FACTS, muted: true }],
    ["a muted, unread session", { ...FACTS, muted: true, unread: true }],
    ["a session in a muted project", { ...FACTS, mutedByProject: true }],
    [
      "a muted, unread session in a muted project",
      { ...FACTS, muted: true, mutedByProject: true, unread: true },
    ],
    [
      "a filed session that isn't running",
      { ...FACTS, currentProject: "Payments", stoppable: false },
    ],
    [
      "a single-user server without nested agents or mute",
      {
        ...FACTS,
        server: { singleUser: true, sharingOff: false, siblingSessions: false, sessionMute: false },
      },
    ],
    [
      "a server with sharing turned off",
      { ...FACTS, owner: false, server: { ...FACTS.server, sharingOff: true } },
    ],
  ] as const)("lists and greys what the row menu does for %s", (_name, facts) => {
    const commands = sessionCommands(facts).filter((command) =>
      isCommandListed(command.availability),
    );
    const menu = rowMenu(facts);
    expect(commands.map((command) => command.label)).toEqual(menu.names);
    expect(
      commands.filter((command) => !command.availability.available).map((command) => command.label),
    ).toEqual(menu.greyed);
  });
});
