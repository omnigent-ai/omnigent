import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PresenceAvatars } from "./PresenceAvatars";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";

// Pin the viewer's identity so the self-filter is deterministic. Keep
// the module's other exports real — chatStore (imported transitively)
// pulls authenticatedFetch from the same module.
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/identity")>()),
  getCurrentAuthorId: () => "self@example.com",
}));

afterEach(() => {
  cleanup();
  useChatStore.setState({ viewers: [] });
});

function renderAvatars() {
  return render(
    <TooltipProvider>
      <PresenceAvatars />
    </TooltipProvider>,
  );
}

describe("PresenceAvatars", () => {
  it("renders nothing when the only viewer is the current user", () => {
    useChatStore.setState({
      viewers: [{ userId: "self@example.com", idle: false }],
    });
    renderAvatars();
    // The stack must be absent from the DOM entirely — presence shows
    // OTHER people; rendering your own circle when alone is noise (and
    // is also what every single-user session would show).
    expect(screen.queryByTestId("presence-avatars")).toBeNull();
  });

  it("shows other viewers' initials and filters self out", () => {
    useChatStore.setState({
      viewers: [
        { userId: "self@example.com", idle: false },
        { userId: "alice.smith@example.com", idle: false },
      ],
    });
    renderAvatars();
    expect(screen.getByTestId("presence-avatars")).toBeTruthy();
    // Radix AvatarFallback renders the initials as text content.
    expect(screen.getByText("AS")).toBeTruthy();
    expect(screen.queryByTestId("presence-avatar-self@example.com")).toBeNull();
  });

  it("dims idle viewers", () => {
    useChatStore.setState({
      viewers: [
        { userId: "alice@example.com", idle: true },
        { userId: "bob@example.com", idle: false },
      ],
    });
    renderAvatars();
    const idleAvatar = screen.getByTestId("presence-avatar-alice@example.com");
    const activeAvatar = screen.getByTestId("presence-avatar-bob@example.com");
    // The idle grey-out is the entire visual meaning of the flag; if the
    // class stops being applied, idle and active viewers are
    // indistinguishable and the feature silently degrades.
    expect(idleAvatar.className).toContain("opacity-40");
    expect(activeAvatar.className).not.toContain("opacity-40");
  });

  it("collapses beyond three viewers into an overflow chip", () => {
    useChatStore.setState({
      viewers: [
        { userId: "a@example.com", idle: false },
        { userId: "b@example.com", idle: false },
        { userId: "c@example.com", idle: false },
        { userId: "d@example.com", idle: false },
        { userId: "e@example.com", idle: false },
      ],
    });
    renderAvatars();
    // 5 others → 3 circles + "+2". An unbounded row would crowd the
    // header's action buttons on busy shared sessions.
    expect(screen.getByTestId("presence-overflow").textContent).toBe("+2");
    expect(screen.getByTestId("presence-avatar-c@example.com")).toBeTruthy();
    expect(screen.queryByTestId("presence-avatar-d@example.com")).toBeNull();
  });
});
