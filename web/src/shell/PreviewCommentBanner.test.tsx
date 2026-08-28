import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as permissions from "@/hooks/usePermissions";
import { PreviewCommentBanner } from "./PreviewCommentBanner";

vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn() }));

beforeEach(() => {
  vi.mocked(permissions.useCanEdit).mockReturnValue(true);
});

afterEach(() => {
  cleanup();
});

describe("PreviewCommentBanner", () => {
  it("renders a switch-to-edit action for editors", () => {
    render(<PreviewCommentBanner conversationId="conv_1" onSwitchToEdit={() => {}} />);
    expect(screen.getByRole("button", { name: /switch to edit mode/i })).toBeDefined();
  });

  it("invokes onSwitchToEdit when the action is clicked", () => {
    const onSwitchToEdit = vi.fn();
    render(<PreviewCommentBanner conversationId="conv_1" onSwitchToEdit={onSwitchToEdit} />);
    fireEvent.click(screen.getByRole("button", { name: /switch to edit mode/i }));
    expect(onSwitchToEdit).toHaveBeenCalledOnce();
  });

  it("renders nothing for read-only viewers", () => {
    vi.mocked(permissions.useCanEdit).mockReturnValue(false);
    const { container } = render(
      <PreviewCommentBanner conversationId="conv_1" onSwitchToEdit={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
