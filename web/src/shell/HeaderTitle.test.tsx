import type { ReactElement } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as ConversationsModule from "@/hooks/useConversations";
import { TooltipProvider } from "@/components/ui/tooltip";
import { HeaderTitle } from "./HeaderTitle";

const mocks = vi.hoisted(() => ({
  rename: vi.fn(),
  pending: false,
}));

vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  return {
    ...actual,
    useRenameConversation: () => ({ mutate: mocks.rename, isPending: mocks.pending }),
  };
});

function renderTitle(ui: ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}

function enterEdit() {
  fireEvent.click(screen.getByTestId("header-title"));
}

beforeEach(() => {
  mocks.pending = false;
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("HeaderTitle", () => {
  it("opens from the rename shortcut with the title selected and saves on Enter", () => {
    renderTitle(<HeaderTitle conversationId="conv-1" title="Planning" />);
    expect(screen.getByTestId("header-title")).toHaveAttribute(
      "aria-keyshortcuts",
      "Control+Alt+R",
    );
    fireEvent.keyDown(window, { code: "KeyR", ctrlKey: true, altKey: true });

    const input = screen.getByRole("textbox", { name: "Session name" }) as HTMLInputElement;
    expect(input).toHaveFocus();
    expect([input.selectionStart, input.selectionEnd]).toEqual([0, "Planning".length]);
    fireEvent.change(input, { target: { value: "Roadmap" } });
    fireEvent.keyDown(input, { code: "KeyR", ctrlKey: true, altKey: true, repeat: true });
    expect(input).toHaveValue("Roadmap");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename).toHaveBeenCalledExactlyOnceWith({ id: "conv-1", title: "Roadmap" });
  });

  it("does not open from the shortcut while a rename is saving", () => {
    mocks.pending = true;
    renderTitle(<HeaderTitle conversationId="conv-1" title="Planning" />);
    fireEvent.keyDown(window, { code: "KeyR", ctrlKey: true, altKey: true });
    expect(screen.queryByRole("textbox", { name: "Session name" })).toBeNull();
  });

  it("shows the title as a button that opens an inline input on click", () => {
    renderTitle(<HeaderTitle conversationId="conv-1" title="Planning" />);
    const trigger = screen.getByTestId("header-title");
    expect(trigger).toHaveTextContent("Planning");

    enterEdit();
    const input = screen.getByRole("textbox", { name: "Session name" });
    expect(input).toHaveValue("Planning");
  });

  it("commits a changed title on Enter", () => {
    renderTitle(<HeaderTitle conversationId="conv-1" title="Planning" />);
    enterEdit();

    const input = screen.getByRole("textbox", { name: "Session name" });
    fireEvent.change(input, { target: { value: "Roadmap" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename).toHaveBeenCalledWith({ id: "conv-1", title: "Roadmap" });
  });

  it("commits on blur and leaves edit mode", () => {
    renderTitle(<HeaderTitle conversationId="conv-1" title="Planning" />);
    enterEdit();

    const input = screen.getByRole("textbox", { name: "Session name" });
    fireEvent.change(input, { target: { value: "Roadmap" } });
    fireEvent.blur(input);
    expect(mocks.rename).toHaveBeenCalledWith({ id: "conv-1", title: "Roadmap" });
    expect(screen.getByTestId("header-title")).toBeInTheDocument();
  });

  it("cancels on Escape without renaming", () => {
    renderTitle(<HeaderTitle conversationId="conv-1" title="Planning" />);
    enterEdit();

    const input = screen.getByRole("textbox", { name: "Session name" });
    fireEvent.change(input, { target: { value: "Roadmap" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(mocks.rename).not.toHaveBeenCalled();
    // Back to the button, showing the original title.
    expect(screen.getByTestId("header-title")).toHaveTextContent("Planning");
  });

  it("does not rename when the title is unchanged or blank", () => {
    renderTitle(<HeaderTitle conversationId="conv-1" title="Planning" />);
    enterEdit();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Session name" }), { key: "Enter" });
    expect(mocks.rename).not.toHaveBeenCalled();

    enterEdit();
    const input = screen.getByRole("textbox", { name: "Session name" });
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename).not.toHaveBeenCalled();
  });
});
