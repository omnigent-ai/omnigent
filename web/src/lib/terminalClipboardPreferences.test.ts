import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getOmnigentServerIdentity } from "./host";
import { applyImportedSettings, collectSettings, readSettingsFile } from "./settingsPortability";
import {
  readTerminalClipboardPreference,
  subscribeTerminalClipboardPreference,
  writeTerminalClipboardPreference,
  type TerminalClipboardPreference,
} from "./terminalClipboardPreferences";

vi.mock("./host", () => ({ getOmnigentServerIdentity: vi.fn() }));

const KEY = 'omnigent:terminal-clipboard:v1:"server-a"';
const OTHER_KEY = 'omnigent:terminal-clipboard:v1:"server-b"';
const subscriptions: (() => void)[] = [];

function subscribe(listener: (value: TerminalClipboardPreference) => void) {
  const unsubscribe = subscribeTerminalClipboardPreference(listener);
  subscriptions.push(unsubscribe);
  return unsubscribe;
}

function storageChange(key: string | null, storageArea = localStorage) {
  window.dispatchEvent(new StorageEvent("storage", { key, storageArea }));
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
});

afterEach(() => {
  vi.unstubAllGlobals();
  subscriptions.splice(0).forEach((unsubscribe) => unsubscribe());
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("terminal clipboard preference", () => {
  it("asks by default and persists allow, block, and revocation", () => {
    expect(readTerminalClipboardPreference()).toBe("ask");
    expect(writeTerminalClipboardPreference("allow")).toBe(true);
    expect(localStorage.getItem(KEY)).toBe("allow");
    expect(readTerminalClipboardPreference()).toBe("allow");
    expect(writeTerminalClipboardPreference("block")).toBe(true);
    expect(readTerminalClipboardPreference()).toBe("block");
    expect(writeTerminalClipboardPreference("ask")).toBe(true);
    expect(localStorage.getItem(KEY)).toBeNull();
    expect(readTerminalClipboardPreference()).toBe("ask");
  });

  it("survives a fresh module load", async () => {
    writeTerminalClipboardPreference("allow");
    vi.resetModules();
    const reloaded = await import("./terminalClipboardPreferences");
    expect(reloaded.readTerminalClipboardPreference()).toBe("allow");
  });

  it("isolates decisions by server identity", () => {
    writeTerminalClipboardPreference("allow");
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-b");
    expect(readTerminalClipboardPreference()).toBe("ask");
    writeTerminalClipboardPreference("block");
    expect(localStorage.getItem(OTHER_KEY)).toBe("block");
    vi.mocked(getOmnigentServerIdentity).mockReturnValue("server-a");
    expect(readTerminalClipboardPreference()).toBe("allow");
  });

  it("cannot persist trust without a known server identity", () => {
    vi.mocked(getOmnigentServerIdentity).mockReturnValue(null);
    expect(readTerminalClipboardPreference()).toBe("ask");
    expect(writeTerminalClipboardPreference("allow")).toBe(false);
    expect(localStorage.length).toBe(0);
  });

  it.each(["", "true", "ALLOW", '"allow"', "invalid"])(
    "asks for an invalid stored value %j",
    (value) => {
      localStorage.setItem(KEY, value);
      expect(readTerminalClipboardPreference()).toBe("ask");
    },
  );

  it("does not persist invalid decisions", () => {
    expect(writeTerminalClipboardPreference("invalid" as TerminalClipboardPreference)).toBe(false);
    expect(localStorage.length).toBe(0);
  });

  it("synchronously notifies same-tab subscribers, including repeated revocations", () => {
    const listener = vi.fn();
    const unsubscribe = subscribe(listener);
    writeTerminalClipboardPreference("allow");
    writeTerminalClipboardPreference("block");
    writeTerminalClipboardPreference("ask");
    writeTerminalClipboardPreference("ask");
    expect(listener.mock.calls).toEqual([["allow"], ["block"], ["ask"], ["ask"]]);

    unsubscribe();
    writeTerminalClipboardPreference("allow");
    storageChange(KEY);
    expect(listener).toHaveBeenCalledTimes(4);
  });

  it("notifies other tabs of grants, blocks, revocation, and storage clearing", () => {
    const listener = vi.fn();
    subscribe(listener);
    localStorage.setItem(KEY, "allow");
    storageChange(KEY);
    localStorage.setItem(KEY, "block");
    storageChange(KEY);
    localStorage.removeItem(KEY);
    storageChange(KEY);
    localStorage.clear();
    storageChange(null);
    expect(listener.mock.calls).toEqual([["allow"], ["block"], ["ask"], ["ask"]]);
  });

  it("ignores unrelated storage keys, other servers, and session storage", () => {
    const listener = vi.fn();
    subscribe(listener);
    storageChange("omnigent:unrelated");
    storageChange(OTHER_KEY);
    storageChange(KEY, sessionStorage);
    expect(listener).not.toHaveBeenCalled();
  });

  it("reads current storage instead of restoring an outdated cross-tab grant", () => {
    const listener = vi.fn();
    subscribe(listener);
    window.dispatchEvent(
      new StorageEvent("storage", { key: KEY, newValue: "allow", storageArea: localStorage }),
    );
    expect(listener).toHaveBeenCalledWith("ask");
  });

  it("asks when local storage cannot be read", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(readTerminalClipboardPreference()).toBe("ask");
  });

  it("reports failed persistence without broadcasting a grant", () => {
    const listener = vi.fn();
    subscribe(listener);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(writeTerminalClipboardPreference("allow")).toBe(false);
    expect(listener).not.toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("ask");
  });

  it("reports failed revocation without claiming the stored grant changed", () => {
    writeTerminalClipboardPreference("allow");
    const listener = vi.fn();
    subscribe(listener);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(writeTerminalClipboardPreference("ask")).toBe(false);
    expect(listener).not.toHaveBeenCalled();
    expect(readTerminalClipboardPreference()).toBe("allow");
  });

  it("safely handles an inaccessible localStorage property", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(readTerminalClipboardPreference()).toBe("ask");
    expect(writeTerminalClipboardPreference("allow")).toBe(false);
  });

  it("does not grant or persist permission without a browser", () => {
    vi.stubGlobal("window", undefined);
    expect(readTerminalClipboardPreference()).toBe("ask");
    expect(writeTerminalClipboardPreference("allow")).toBe(false);
    expect(subscribeTerminalClipboardPreference(vi.fn())).toBeTypeOf("function");
  });

  it("never exports clipboard trust or restores it from imported settings", async () => {
    writeTerminalClipboardPreference("allow");
    expect(collectSettings()?.settings).not.toHaveProperty(KEY);

    const imported = { version: 1, settings: { [KEY]: "allow" } };
    writeTerminalClipboardPreference("ask");
    expect(applyImportedSettings(imported)).toBe(0);
    expect(readTerminalClipboardPreference()).toBe("ask");
    await expect(
      readSettingsFile(new File([JSON.stringify(imported)], "settings.json")),
    ).rejects.toThrow("valid Omnigent settings");
  });
});
