import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useWebUpdateNotifications } from "@/hooks/useWebUpdateNotifications";
import { WebUpdateBanner } from "./WebUpdateBanner";

vi.mock("@/hooks/useWebUpdateNotifications", () => ({ useWebUpdateNotifications: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows the confirmed update and lets Later dismiss without reloading", () => {
  const dismiss = vi.fn();
  vi.mocked(useWebUpdateNotifications).mockReturnValue({ availableBuildId: null, dismiss });
  const { rerender } = render(<WebUpdateBanner />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  vi.mocked(useWebUpdateNotifications).mockReturnValue({ availableBuildId: "new", dismiss });
  rerender(<WebUpdateBanner />);
  expect(screen.getByRole("status", { name: "Web app update" })).toHaveTextContent(
    "Update available",
  );
  fireEvent.click(screen.getByRole("button", { name: "Later" }));
  expect(dismiss).toHaveBeenCalledOnce();
  vi.mocked(useWebUpdateNotifications).mockReturnValue({ availableBuildId: null, dismiss });
  rerender(<WebUpdateBanner />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("reloads the document only when Reload is clicked", () => {
  const reload = vi.fn();
  const testWindow = Object.create(window);
  Object.defineProperty(testWindow, "location", { value: { reload } });
  vi.stubGlobal("window", testWindow);
  vi.mocked(useWebUpdateNotifications).mockReturnValue({
    availableBuildId: "new",
    dismiss: vi.fn(),
  });
  render(<WebUpdateBanner />);
  expect(reload).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Reload" }));
  expect(reload).toHaveBeenCalledOnce();
});
