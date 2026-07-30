import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockUseServiceWorkerUpdate, reload, dismiss } = vi.hoisted(() => ({
  mockUseServiceWorkerUpdate: vi.fn(),
  reload: vi.fn(),
  dismiss: vi.fn(),
}));

vi.mock("./useServiceWorkerUpdate", () => ({
  useServiceWorkerUpdate: mockUseServiceWorkerUpdate,
}));

import { PWAUpdateBanner } from "./PWAUpdateBanner";

function mockUpdate(state: { needRefresh: boolean }): void {
  mockUseServiceWorkerUpdate.mockReturnValue({
    needRefresh: state.needRefresh,
    reload,
    dismiss,
  });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("PWAUpdateBanner", () => {
  it("renders nothing when no update is available", () => {
    mockUpdate({ needRefresh: false });
    const { container } = render(<PWAUpdateBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("offers Reload that applies the update when a new version is available", () => {
    mockUpdate({ needRefresh: true });
    render(<PWAUpdateBanner />);
    expect(screen.getByText(/new version/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /reload/i }));
    expect(reload).toHaveBeenCalledOnce();
  });

  it("dismisses the prompt", () => {
    mockUpdate({ needRefresh: true });
    render(<PWAUpdateBanner />);
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(dismiss).toHaveBeenCalledOnce();
  });
});
