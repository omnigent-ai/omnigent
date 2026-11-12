import { afterEach, expect, it, vi } from "vitest";
import { isAdminViewEnabled, setAdminViewEnabled } from "./adminView";

afterEach(() => {
  document.cookie = "omnigent_admin_view=; Max-Age=0; Path=/";
  vi.unstubAllGlobals();
});

it.each([true, false])("sets admin view to %s and reloads the page", (enabled) => {
  const reload = vi.fn();
  vi.stubGlobal("location", { protocol: "http:", reload });
  expect(isAdminViewEnabled()).toBe(true);
  setAdminViewEnabled(enabled);
  expect(isAdminViewEnabled()).toBe(enabled);
  expect(reload).toHaveBeenCalledOnce();
});
