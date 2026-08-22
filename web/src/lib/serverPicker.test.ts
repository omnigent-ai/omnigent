import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  connectWebServer,
  getServerPicker,
  normalizeServerUrl,
  openServerSetup,
  switchServer,
} from "./serverPicker";

const getNativeServerPicker = vi.fn();
const isNativeShell = vi.fn();
const openNativeServerSetup = vi.fn();
const switchNativeServer = vi.fn();
const getOmnigentHostConfig = vi.fn();

vi.mock("./nativeBridge", () => ({
  getServerPicker: () => getNativeServerPicker(),
  isNativeShell: () => isNativeShell(),
  openServerSetup: () => openNativeServerSetup(),
  switchServer: (url: string) => switchNativeServer(url),
}));

vi.mock("./host", () => ({
  getOmnigentHostConfig: () => getOmnigentHostConfig(),
}));

const originalLocation = window.location;
let hrefWrites: string[];

function setLocation(hash = "") {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      origin: "http://localhost:6767",
      pathname: "/",
      search: "",
      hash,
      set href(value: string) {
        hrefWrites.push(value);
      },
    },
  });
}

beforeEach(() => {
  hrefWrites = [];
  localStorage.clear();
  getNativeServerPicker.mockReset().mockResolvedValue(null);
  isNativeShell.mockReset().mockReturnValue(false);
  openNativeServerSetup.mockReset();
  switchNativeServer.mockReset();
  getOmnigentHostConfig.mockReset().mockReturnValue({});
  vi.spyOn(window.history, "replaceState").mockImplementation(() => {});
  setLocation();
});

afterEach(() => {
  vi.restoreAllMocks();
  Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
});

describe("normalizeServerUrl", () => {
  it("defaults loopback to HTTP and remote hosts to HTTPS", () => {
    expect(normalizeServerUrl("localhost:6771")).toBe("http://localhost:6771/");
    expect(normalizeServerUrl("example.com/path")).toBe("https://example.com/path");
  });

  it("rejects empty, invalid, and non-HTTP URLs", () => {
    expect(() => normalizeServerUrl(" ")).toThrow("Enter a server URL");
    expect(() => normalizeServerUrl("http://[")).toThrow("valid server URL");
    expect(() => normalizeServerUrl("file:///tmp/server")).toThrow("HTTP or HTTPS");
  });
});

describe("server picker runtime", () => {
  it("uses Electron picker data and actions when available", async () => {
    getNativeServerPicker.mockResolvedValue({
      currentOrigin: "http://localhost:6767",
      recentServers: ["http://localhost:6771/"],
    });

    await expect(getServerPicker()).resolves.toEqual({
      currentOrigin: "http://localhost:6767",
      recentServers: ["http://localhost:6771/"],
      runtime: "desktop",
    });
    await switchServer("http://localhost:6771/", "desktop");
    openServerSetup();
    expect(switchNativeServer).toHaveBeenCalledWith("http://localhost:6771/");
    expect(openNativeServerSetup).toHaveBeenCalled();
  });

  it("shows standalone web and remembers transferred recents", async () => {
    setLocation(
      `#omnigent-server-recents=${encodeURIComponent(JSON.stringify(["http://localhost:6771/"]))}`,
    );

    await expect(getServerPicker()).resolves.toEqual({
      currentOrigin: "http://localhost:6767/",
      recentServers: ["http://localhost:6767/", "http://localhost:6771/"],
      runtime: "web",
    });
    expect(window.history.replaceState).toHaveBeenCalledWith(null, "", "/");
  });

  it("stays hidden in embedded and non-Electron native runtimes", async () => {
    getOmnigentHostConfig.mockReturnValue({ fetcher: vi.fn() });
    await expect(getServerPicker()).resolves.toBeNull();
    getOmnigentHostConfig.mockReturnValue({});
    isNativeShell.mockReturnValue(true);
    await expect(getServerPicker()).resolves.toBeNull();
  });

  it("navigates web switches with the recent list in the URL fragment", async () => {
    await switchServer("localhost:6771", "web");
    connectWebServer("https://example.com/omnigent");

    expect(new URL(hrefWrites[0]).origin).toBe("http://localhost:6771");
    expect(decodeURIComponent(new URL(hrefWrites[0]).hash)).toContain("localhost:6767");
    expect(new URL(hrefWrites[1]).origin).toBe("https://example.com");
  });
});
