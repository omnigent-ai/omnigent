import type { ReactElement } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as ConversationsModule from "@/hooks/useConversations";
import { TooltipProvider } from "@/components/ui/tooltip";
import { HeaderProjectTag } from "./HeaderProjectTag";

const mocks = vi.hoisted(() => ({
  projects: [{ id: "project-1", name: "Sprint 42" }],
  moveToProject: vi.fn(),
}));

vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  return {
    ...actual,
    useProjects: () => ({ data: mocks.projects }),
    useMoveToProject: () => ({ mutate: mocks.moveToProject }),
  };
});

function renderTag(ui: ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}

function openTag() {
  fireEvent.pointerDown(screen.getByTestId("header-project-tag"), { button: 0 });
}

beforeEach(() => {
  mocks.projects = [{ id: "project-1", name: "Sprint 42" }];
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("HeaderProjectTag", () => {
  it("labels the trigger by project when filed and by intent when unfiled", () => {
    const { rerender } = renderTag(
      <HeaderProjectTag conversationId="conv-1" projectName="Payments" />,
    );
    expect(screen.getByTestId("header-project-tag")).toHaveAttribute(
      "aria-label",
      "Project: Payments",
    );

    rerender(
      <TooltipProvider>
        <HeaderProjectTag conversationId="conv-1" projectName={null} />
      </TooltipProvider>,
    );
    expect(screen.getByTestId("header-project-tag")).toHaveAttribute(
      "aria-label",
      "Add to project",
    );
  });

  it("shows the current project in the tooltip when filed, else invites the move", () => {
    const { rerender } = renderTag(
      <HeaderProjectTag conversationId="conv-1" projectName="Payments" />,
    );
    // Radix opens the tooltip immediately on focus (no hover delay) and renders
    // the content into a portal; getAllByText covers the visually-hidden a11y
    // copy Radix mirrors alongside the visible one.
    fireEvent.focus(screen.getByTestId("header-project-tag"));
    expect(screen.getAllByText("Currently in: Payments").length).toBeGreaterThan(0);

    rerender(
      <TooltipProvider>
        <HeaderProjectTag conversationId="conv-1" projectName={null} />
      </TooltipProvider>,
    );
    fireEvent.focus(screen.getByTestId("header-project-tag"));
    expect(screen.getAllByText("Move session").length).toBeGreaterThan(0);
  });

  it("opens the picker and moves the session to the chosen project", () => {
    renderTag(<HeaderProjectTag conversationId="conv-1" projectName={null} />);
    openTag();

    expect(screen.getByRole("textbox", { name: "Search or create project" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Sprint 42" }));
    expect(mocks.moveToProject).toHaveBeenCalledWith({ id: "conv-1", project: "Sprint 42" });
  });

  it("creates and files into a new project from the typed name", () => {
    renderTag(<HeaderProjectTag conversationId="conv-1" projectName={null} />);
    openTag();

    fireEvent.change(screen.getByRole("textbox", { name: "Search or create project" }), {
      target: { value: "Roadmap" },
    });
    fireEvent.click(screen.getByRole("menuitem", { name: /Create Roadmap/ }));
    expect(mocks.moveToProject).toHaveBeenCalledWith({ id: "conv-1", project: "Roadmap" });
  });

  it("offers no create row when the typed name already exists", () => {
    renderTag(<HeaderProjectTag conversationId="conv-1" projectName={null} />);
    openTag();

    fireEvent.change(screen.getByRole("textbox", { name: "Search or create project" }), {
      target: { value: "sprint 42" },
    });
    expect(screen.queryByRole("menuitem", { name: /Create/ })).toBeNull();
  });

  it("offers a remove option that unfiles the session", () => {
    renderTag(
      <HeaderProjectTag
        conversationId="conv-1"

        projectName="Sprint 42"
      />,
    );
    openTag();

    fireEvent.click(screen.getByRole("menuitem", { name: /Remove from/ }));
    expect(mocks.moveToProject).toHaveBeenCalledWith({ id: "conv-1", project: "" });
  });

  it("renders the project's emoji icon at full opacity when one is set", () => {
    renderTag(<HeaderProjectTag conversationId="conv-1" projectName="Payments" projectIcon="🚀" />);
    // The emoji glyph shows instead of the folder icon, and the trigger is not
    // dimmed — a faded full-color emoji reads as washed-out.
    expect(screen.getByTestId("project-icon")).toHaveTextContent("🚀");
    expect(screen.getByTestId("header-project-tag").className).toContain("opacity-100");
    expect(screen.getByTestId("header-project-tag").className).not.toContain("opacity-40");
  });

  it("dims the folder glyph when the filed project has no icon", () => {
    renderTag(<HeaderProjectTag conversationId="conv-1" projectName="Payments" />);
    expect(screen.queryByTestId("project-icon")).toBeNull();
    expect(screen.getByTestId("header-project-tag").className).toContain("opacity-40");
  });
});
