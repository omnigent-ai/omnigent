import { afterEach, expect, it, vi } from "vitest";
import { isAdminViewEnabled, setAdminViewEnabled } from "./adminView";

afterEach(() => {
  vi.restoreAllMocks();
  document.cookie = "omnigent_admin_view=; Max-Age=0; Path=/";
  vi.unstubAllGlobals();
});

it.each([true, false])("sets admin view to %s and reloads the page", (enabled) => {
  const reload = vi.fn();
  const writeCookie = vi.spyOn(document, "cookie", "set");
  vi.stubGlobal("location", { protocol: "http:", reload });
  if (enabled) document.cookie = "omnigent_admin_view=off; Path=/";
  expect(isAdminViewEnabled()).toBe(!enabled);
  setAdminViewEnabled(enabled);
  expect(writeCookie).toHaveBeenCalledWith(expect.stringContaining("Max-Age=31536000"));
  expect(isAdminViewEnabled()).toBe(enabled);
  expect(reload).toHaveBeenCalledOnce();
});

it("reports refused cookie writes without reloading", () => {
  const reload = vi.fn();
  vi.stubGlobal("location", { protocol: "http:", reload });
  vi.spyOn(document, "cookie", "set").mockImplementation(() => {});
  expect(() => setAdminViewEnabled(false)).toThrow("Allow browser cookies");
  expect(reload).not.toHaveBeenCalled();
});
