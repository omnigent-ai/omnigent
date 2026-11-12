import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as bridge from "@/lib/nativeBridge";
import { SettingsNotificationsSection } from "./SettingsNotificationsSection";

vi.mock("@/lib/nativeBridge", () => ({
  getNativeNotificationSettings: vi.fn(),
  setNativeNotificationSettings: vi.fn(),
  onNativeNotificationModeChanged: vi.fn(() => () => {}),
  previewNativeNotificationSound: vi.fn().mockResolvedValue(undefined),
  openNativeNotificationSettings: vi.fn().mockResolvedValue(undefined),
  nativeNotify: vi.fn().mockResolvedValue(true),
}));

const defaults: bridge.NativeNotificationSettings = {
  mode: "when-away",
  soundEnabled: false,
  soundName: "Glass",
  sounds: ["Glass", "Ping"],
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(bridge.getNativeNotificationSettings).mockResolvedValue(defaults);
  vi.mocked(bridge.setNativeNotificationSettings).mockImplementation(async (patch) => ({
    ...defaults,
    ...patch,
  }));
});
afterEach(cleanup);

it("loads saved controls, saves Always, and refreshes changes from another window", async () => {
  render(<SettingsNotificationsSection />);
  const always = await screen.findByRole("radio", { name: /Always/ });
  expect(always).not.toBeChecked();
  fireEvent.click(always);
  await waitFor(() => expect(always).toBeChecked());
  expect(bridge.setNativeNotificationSettings).toHaveBeenCalledWith({ mode: "always" });
  await act(async () => {
    vi.mocked(bridge.onNativeNotificationModeChanged).mock.calls[0][0]("when-away");
  });
  expect(always).not.toBeChecked();
});

it("previews while sound is off, enables sound, sends a test, and opens system settings", async () => {
  render(<SettingsNotificationsSection />);
  const toggle = await screen.findByRole("switch", { name: "Play notification sound" });
  expect(toggle).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "Preview sound" }));
  await waitFor(() => expect(bridge.previewNativeNotificationSound).toHaveBeenCalledWith("Glass"));
  await waitFor(() => expect(toggle).toBeEnabled());
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle).toBeChecked());
  expect(bridge.setNativeNotificationSettings).toHaveBeenCalledWith({ soundEnabled: true });
  fireEvent.click(screen.getByRole("button", { name: "Send test notification" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Test sent");
  expect(bridge.nativeNotify).toHaveBeenCalledWith(
    expect.objectContaining({ navigatePath: "/settings/notifications" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Open macOS notification settings" }));
  await waitFor(() => expect(bridge.openNativeNotificationSettings).toHaveBeenCalledOnce());
});

it("keeps the saved selection and reports a failed save", async () => {
  vi.mocked(bridge.setNativeNotificationSettings).mockRejectedValue(new Error("IPC failed"));
  render(<SettingsNotificationsSection />);
  const always = await screen.findByRole("radio", { name: /Always/ });
  fireEvent.click(always);
  expect(await screen.findByRole("alert")).toHaveTextContent("Please try again");
  expect(always).not.toBeChecked();
});

it("explains unsupported shells without showing working-looking controls", async () => {
  vi.mocked(bridge.getNativeNotificationSettings).mockResolvedValue(null);
  render(<SettingsNotificationsSection />);
  expect(await screen.findByText(/up-to-date Omnigent desktop app/)).toBeInTheDocument();
  expect(screen.queryByRole("radio")).not.toBeInTheDocument();
});
