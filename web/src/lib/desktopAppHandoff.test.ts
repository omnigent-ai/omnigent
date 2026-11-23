import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import {
  continueInBrowser,
  DESKTOP_OPEN_PORTS,
  desktopAppDeepLink,
  handedOverConversationId,
  handOffToDesktopApp,
  desktopAppRunning,
  openInDesktopApp,
  readOpenInDesktopApp,
  writeOpenInDesktopApp,
} from "@/lib/desktopAppHandoff";

vi.mock("sonner", () => ({ toast: vi.fn() }));

const CONVERSATION_ID = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9";

/** The ports, in order, that `fetch` was called with. */
function portsAsked(fetchMock: ReturnType<typeof vi.fn>): number[] {
  return fetchMock.mock.calls.map(([url]) => Number(new URL(String(url)).port));
}

/** The app's own answer: every one carries the listener's header. */
function appAnswer(status: number): Response {
  return new Response(null, { status, headers: { "X-Omnigent-Desktop": "1" } });
}

/**
 * A desktop app answering on `port`. Each port in `otherPrograms` holds another
 * program that answers readably; every remaining port refuses the connection.
 */
function appOn(port: number | null, status = 204, otherPrograms: number[] = []) {
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    const asked = Number(new URL(url).port);
    if (asked === port) return appAnswer(status);
    if (otherPrograms.includes(asked)) return new Response("Not Found", { status: 404 });
    throw new TypeError("Failed to fetch");
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The requests that asked the app to open a conversation, leaving out "are you running". */
function openRequests(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .map(([url]) => String(url))
    .filter((url) => url.endsWith("/open-conversation"));
}

/** Serve the page from `hostname`, as the permission check sees it. */
function pageOn(hostname: string, protocol = "https:") {
  vi.stubGlobal("location", { ...window.location, hostname, host: hostname, protocol });
}

/** The browser's answer for the loopback permission; `null` is a browser without one. */
function browserPermission(state: PermissionState | null) {
  const query = vi.fn(async () => {
    if (state === null) throw new TypeError("unknown permission name");
    return { state };
  });
  vi.stubGlobal("navigator", { userAgent: navigator.userAgent, permissions: { query } });
  return query;
}

/** The toast offering the hand-off, if one was shown. */
function offer() {
  const call = vi.mocked(toast).mock.calls.find(([message]) => String(message).includes("?"));
  return call?.[1] as
    { description: string; action: { label: string; onClick: () => void } } | undefined;
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  continueInBrowser();
  vi.mocked(toast).mockReset();
  pageOn("omnigent.example.com");
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("openInDesktopApp", () => {
  it("asks each port in turn and stops at the app's answer", async () => {
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[1]);

    expect(await openInDesktopApp(CONVERSATION_ID)).toBe(true);

    expect(portsAsked(fetchMock)).toEqual(DESKTOP_OPEN_PORTS.slice(0, 2));
    const [url, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe(`http://127.0.0.1:${DESKTOP_OPEN_PORTS[1]}/open-conversation`);
    expect(init.method).toBe("POST");
    // Only the id travels; the app takes the server from the browser's Origin header.
    expect(JSON.parse(String(init.body))).toEqual({ conversationId: CONVERSATION_ID });
    expect(init.credentials).toBe("omit");
  });

  it("passes over a port another program answers on, to find the app on the next", async () => {
    const [taken, appPort] = DESKTOP_OPEN_PORTS;
    const fetchMock = appOn(appPort, 204, [taken]);

    expect(await openInDesktopApp(CONVERSATION_ID)).toBe(true);
    expect(portsAsked(fetchMock)).toEqual([taken, appPort]);

    // The "is the app running" probe looks past it the same way.
    fetchMock.mockClear();
    expect(await desktopAppRunning()).toBe(true);
    expect(portsAsked(fetchMock)).toEqual([taken, appPort]);

    // Another program's success is not the app's.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 204 })),
    );
    expect(await openInDesktopApp(CONVERSATION_ID)).toBe(false);
    expect(await desktopAppRunning()).toBe(false);
  });

  it("reports no hand-off when nothing answers, or the app declines the request", async () => {
    const silent = appOn(null);
    expect(await openInDesktopApp(CONVERSATION_ID)).toBe(false);
    expect(portsAsked(silent)).toEqual(DESKTOP_OPEN_PORTS);

    // Declining (here: asked again too soon) is the app's answer, so no other port is asked.
    const refusing = appOn(DESKTOP_OPEN_PORTS[0], 429);
    expect(await openInDesktopApp(CONVERSATION_ID)).toBe(false);
    expect(portsAsked(refusing)).toEqual([DESKTOP_OPEN_PORTS[0]]);
  });
});

describe("handOffToDesktopApp", () => {
  it.each([
    [
      "the browser's permission is granted",
      "omnigent.example.com",
      "granted" as const,
      "Links you",
    ],
    ["the browser has no such permission", "omnigent.example.com", null, "Links you"],
    ["the server is on this machine", "localhost", "prompt" as const, "Links you"],
    ["the browser will ask too", "omnigent.example.com", "prompt" as const, "browser will ask"],
  ])("asks first, opening nothing in the app, when %s", async (_label, host, state, says) => {
    pageOn(host);
    browserPermission(state);
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[0]);

    await handOffToDesktopApp(CONVERSATION_ID);

    expect(openRequests(fetchMock)).toEqual([]);
    expect(offer()?.action.label).toBe("Open in app");
    expect(offer()?.description).toContain(says);
    expect(readOpenInDesktopApp()).toBe(false);

    // Accepting turns the preference on and opens this conversation.
    offer()?.action.onClick();
    await vi.waitFor(() => expect(handedOverConversationId()).toBe(CONVERSATION_ID));
    expect(openRequests(fetchMock)).toHaveLength(1);
    expect(readOpenInDesktopApp()).toBe(true);
  });

  it("doesn't ask someone who isn't running the app, and asks once they are", async () => {
    browserPermission("granted");
    appOn(null);
    await handOffToDesktopApp(CONVERSATION_ID);
    expect(toast).not.toHaveBeenCalled();

    appOn(DESKTOP_OPEN_PORTS[0]);
    await handOffToDesktopApp(CONVERSATION_ID);
    expect(offer()).toBeDefined();
  });

  it("can't look for the app before the browser's own prompt, so it asks anyway", async () => {
    browserPermission("prompt");
    const fetchMock = appOn(null);

    await handOffToDesktopApp(CONVERSATION_ID);

    // Any request here would bring up the browser's prompt unasked.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(offer()).toBeDefined();
  });

  it("asks once when called twice before the first decision is made", async () => {
    browserPermission("granted");
    appOn(DESKTOP_OPEN_PORTS[0]);

    // React's development double-run of an effect calls it twice in a row.
    await Promise.all([handOffToDesktopApp(CONVERSATION_ID), handOffToDesktopApp(CONVERSATION_ID)]);

    expect(
      vi.mocked(toast).mock.calls.filter(([message]) => String(message).includes("?")),
    ).toHaveLength(1);
  });

  it("reads the permission under the second name when the browser doesn't know the first", async () => {
    const query = vi.fn(async ({ name }: { name: string }) => {
      if (name === "loopback-network") throw new TypeError("unknown permission name");
      return { state: "prompt" };
    });
    vi.stubGlobal("navigator", { userAgent: navigator.userAgent, permissions: { query } });
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[0]);

    await handOffToDesktopApp(CONVERSATION_ID);

    expect(query.mock.calls.map(([descriptor]) => descriptor.name)).toEqual([
      "loopback-network",
      "local-network-access",
    ]);
    // "prompt" under the second name: no request may go out before a click.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(offer()?.description).toContain("browser will ask");
  });

  it("asks only once when the question is ignored", async () => {
    browserPermission("granted");
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[0]);

    await handOffToDesktopApp(CONVERSATION_ID);
    vi.mocked(toast).mockReset();
    fetchMock.mockClear();
    await handOffToDesktopApp(CONVERSATION_ID);

    expect(toast).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("opens at once after the user has accepted, and not again on reload", async () => {
    writeOpenInDesktopApp(true);
    browserPermission("granted");
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[0]);

    await handOffToDesktopApp(CONVERSATION_ID);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    // The page says so in place of the conversation, not with a toast.
    expect(handedOverConversationId()).toBe(CONVERSATION_ID);
    expect(toast).not.toHaveBeenCalled();

    // Staying in the browser holds across a reload of the tab.
    continueInBrowser();
    await handOffToDesktopApp(CONVERSATION_ID);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(handedOverConversationId()).toBeNull();
  });

  it("stays silent after acceptance when the app isn't running", async () => {
    writeOpenInDesktopApp(true);
    browserPermission("granted");
    appOn(null);

    await handOffToDesktopApp(CONVERSATION_ID);

    expect(toast).not.toHaveBeenCalled();
    expect(handedOverConversationId()).toBeNull();
  });

  it("offers again, on every visit, while the browser's own permission is still to ask", async () => {
    writeOpenInDesktopApp(true);
    browserPermission("prompt");
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[0]);

    await handOffToDesktopApp(CONVERSATION_ID);
    await handOffToDesktopApp(CONVERSATION_ID);

    // Only a click may bring up the browser's prompt.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      vi.mocked(toast).mock.calls.filter(([message]) => String(message).includes("?")),
    ).toHaveLength(2);
  });

  it("says so when the user accepts the offer but the app can't be reached", async () => {
    browserPermission("granted");
    appOn(DESKTOP_OPEN_PORTS[0]);
    await handOffToDesktopApp(CONVERSATION_ID);

    appOn(null);
    offer()?.action.onClick();

    await vi.waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.stringContaining("Couldn't reach")),
    );
  });

  it.each([
    ["the browser denied the permission", () => browserPermission("denied")],
    ["the permission state can't be read", () => vi.stubGlobal("navigator", { userAgent: "" })],
    ["the preference is off", () => (browserPermission("granted"), writeOpenInDesktopApp(false))],
    [
      "the page is already inside the desktop app",
      () => {
        browserPermission("granted");
        Object.assign(window, { omnigentDesktop: { kind: "electron" } });
      },
    ],
    [
      "the browser is on a phone",
      () => {
        browserPermission("granted");
        vi.stubGlobal("navigator", {
          userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148",
          permissions: navigator.permissions,
        });
      },
    ],
  ])("does nothing when %s", async (_label, arrange) => {
    arrange();
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[0]);

    await handOffToDesktopApp(CONVERSATION_ID);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });
});

describe("desktopAppDeepLink", () => {
  it("names the conversation on this page's server", () => {
    expect(desktopAppDeepLink(CONVERSATION_ID)).toBe(
      `omnigent://omnigent.example.com/c/${CONVERSATION_ID}`,
    );
    pageOn("localhost", "http:");
    expect(desktopAppDeepLink(CONVERSATION_ID)).toBe(`omnigent://localhost/c/${CONVERSATION_ID}`);
  });

  it("has no link for a remote server over plain http", () => {
    pageOn("omnigent.example.com", "http:");
    expect(desktopAppDeepLink(CONVERSATION_ID)).toBeNull();
  });
});

describe("the preference", () => {
  it("is off until the user turns it on", () => {
    expect(readOpenInDesktopApp()).toBe(false);
    writeOpenInDesktopApp(true);
    expect(readOpenInDesktopApp()).toBe(true);
    writeOpenInDesktopApp(false);
    expect(readOpenInDesktopApp()).toBe(false);
  });
});
