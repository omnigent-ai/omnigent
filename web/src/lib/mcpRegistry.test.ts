import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authorizeRegistryService, type RegistryService } from "./mcpRegistry";

const service: RegistryService = {
  id: "tracker",
  title: "Tracker",
  description: "",
  auth: "oauth",
  connected: false,
  tools: [],
  connect_provider: "mcp-tracker",
};
let popup: {
  closed: boolean;
  opener: unknown;
  location: { href: string };
  close: ReturnType<typeof vi.fn>;
};
beforeEach(() => {
  vi.useFakeTimers();
  popup = { closed: false, opener: window, location: { href: "about:blank" }, close: vi.fn() };
  vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete window.__OMNIGENT_BASE_PATH__;
});

it("waits for the server callback, honors a base path, and isolates the opener", async () => {
  window.__OMNIGENT_BASE_PATH__ = "/omni";
  const result = authorizeRegistryService(service, new AbortController().signal);
  expect(popup.opener).toBeNull();
  expect(popup.location.href).toContain(
    "/omni/v1/connections/mcp-tracker/connect?return_to=%2Fomni%2Fsettings%2Fmcp",
  );
  popup.location.href = "https://provider.example/authorize?mcp-tracker=connected";
  await vi.advanceTimersByTimeAsync(500);
  expect(popup.close).not.toHaveBeenCalled();
  popup.location.href = `${window.location.origin}/omni/settings/mcp?mcp-tracker=connected`;
  await vi.advanceTimersByTimeAsync(500);
  await expect(result).resolves.toBeUndefined();
  expect(popup.close).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});

it("reports the server's OAuth rejection", async () => {
  const result = authorizeRegistryService(service, new AbortController().signal);
  const assertion = expect(result).rejects.toThrow("Sign-in failed");
  popup.location.href = `${window.location.origin}/settings/mcp?mcp-tracker=error`;
  await vi.advanceTimersByTimeAsync(500);
  await assertion;
});

it("handles a closed or blocked popup without enabling a service", async () => {
  const result = authorizeRegistryService(service, new AbortController().signal);
  const assertion = expect(result).rejects.toThrow("Sign-in cancelled");
  popup.closed = true;
  await vi.advanceTimersByTimeAsync(500);
  await assertion;
  vi.mocked(window.open).mockReturnValue(null);
  await expect(authorizeRegistryService(service, new AbortController().signal)).rejects.toThrow(
    "Allow pop-ups",
  );
});

it("closes the popup and cancels polling when the picker unmounts", async () => {
  const controller = new AbortController();
  const result = authorizeRegistryService(service, controller.signal);
  const assertion = expect(result).rejects.toThrow("Sign-in cancelled");
  controller.abort();
  await assertion;
  expect(popup.close).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});
