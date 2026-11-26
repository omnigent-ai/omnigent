import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import { applyDesktopAppAttribute } from "@/lib/desktopAppLayout";
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

/** The ports, in order, that were asked whether the app is there. */
function portsAsked(fetchMock: ReturnType<typeof vi.fn>): number[] {
  return fetchMock.mock.calls
    .map(([url]) => new URL(String(url)))
    .filter((url) => url.pathname === "/status")
    .map((url) => Number(url.port));
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
    // The app says it is there; `status` is its answer to opening a conversation.
    if (asked === port) return appAnswer(url.endsWith("/status") ? 204 : status);
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
  delete document.documentElement.dataset.sessionWindow;
  delete document.documentElement.dataset.sessionWindowFor;
  delete document.documentElement.dataset.desktopApp;
});

describe("openInDesktopApp", () => {
  it("finds the app's port, then asks it to open the conversation", async () => {
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[1]);

    expect(await openInDesktopApp(CONVERSATION_ID)).toBe(true);

    expect(portsAsked(fetchMock)).toEqual(DESKTOP_OPEN_PORTS.slice(0, 2));
    expect(openRequests(fetchMock)).toEqual([
      `http://127.0.0.1:${DESKTOP_OPEN_PORTS[1]}/open-conversation`,
    ]);
    const [, init] = fetchMock.mock.calls.at(-1) as unknown as [string, RequestInit];
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
    expect(await desktopAppRunning()).toBe(true);

    // Another program's success is not the app's.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 204 })),
    );
    expect(await openInDesktopApp(CONVERSATION_ID)).toBe(false);
    expect(await desktopAppRunning()).toBe(false);
  });

  it("waits for the app past the time it gives a port to answer, then gives up", async () => {
    vi.useFakeTimers();
    // The platform's own timeout signal doesn't run on the fake clock.
    const timeout = vi.spyOn(AbortSignal, "timeout").mockImplementation((ms) => {
      const controller = new AbortController();
      setTimeout(() => controller.abort(), ms);
      return controller.signal;
    });
    try {
      let answer: (response: Response) => void = () => {};
      const slowApp = () =>
        vi.stubGlobal(
          "fetch",
          vi.fn((url: string, init?: RequestInit) => {
            if (url.endsWith("/status")) return Promise.resolve(appAnswer(204));
            // The app holds its answer until a window shows the conversation.
            return new Promise<Response>((resolve, reject) => {
              answer = resolve;
              init?.signal?.addEventListener("abort", () => reject(new Error("timed out")));
            });
          }),
        );

      slowApp();
      const opened = openInDesktopApp(CONVERSATION_ID);
      // Well past the 1.5 s a port gets to say the app is there.
      await vi.advanceTimersByTimeAsync(5000);
      answer(appAnswer(204));
      expect(await opened).toBe(true);

      slowApp();
      const abandoned = openInDesktopApp(CONVERSATION_ID);
      await vi.advanceTimersByTimeAsync(13_000);
      expect(await abandoned).toBe(false);
    } finally {
      timeout.mockRestore();
      vi.useRealTimers();
    }
  });

  it("opens nothing when no app answers", async () => {
    const silent = appOn(null);

    expect(await openInDesktopApp(CONVERSATION_ID)).toBe(false);

    expect(portsAsked(silent)).toEqual(DESKTOP_OPEN_PORTS);
    expect(openRequests(silent)).toEqual([]);
  });

  it.each([
    ["could not bring the conversation up", 504],
    ["was asked again too soon", 429],
  ])("reports no hand-off when the app %s", async (_label, status) => {
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[0], status);

    expect(await openInDesktopApp(CONVERSATION_ID)).toBe(false);
    // That is the app's answer, so it is asked once.
    expect(openRequests(fetchMock)).toHaveLength(1);
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

  it("leaves the conversation in the tab when the app couldn't bring it up", async () => {
    writeOpenInDesktopApp(true);
    browserPermission("granted");
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[0], 504);

    await handOffToDesktopApp(CONVERSATION_ID);

    expect(openRequests(fetchMock)).toHaveLength(1);
    expect(handedOverConversationId()).toBeNull();
    expect(toast).not.toHaveBeenCalled();
  });

  it.each([
    ["an ordinary conversation link", "", false, true],
    ["a session pop-out", "?window=session", true, false],
    ["a copied pop-out link", "?window=session", false, true],
    ["an ordinary link with an opener", "", true, true],
  ])("with handoff enabled, handles %s", async (_label, search, hasOpener, shouldHandOff) => {
    vi.stubGlobal("location", {
      ...window.location,
      pathname: `/c/${CONVERSATION_ID}`,
      search,
    });
    vi.stubGlobal("opener", hasOpener ? window : null);
    applyDesktopAppAttribute();
    writeOpenInDesktopApp(true);
    browserPermission("granted");
    const fetchMock = appOn(DESKTOP_OPEN_PORTS[0]);

    await handOffToDesktopApp(CONVERSATION_ID);

    expect(portsAsked(fetchMock)).toHaveLength(shouldHandOff ? 1 : 0);
    expect(openRequests(fetchMock)).toHaveLength(shouldHandOff ? 1 : 0);
    // The page says so in place of the conversation, not with a toast.
    expect(handedOverConversationId()).toBe(shouldHandOff ? CONVERSATION_ID : null);
    expect(toast).not.toHaveBeenCalled();
    expect(readOpenInDesktopApp()).toBe(true);

    // Staying in the browser holds across a reload of the tab.
    continueInBrowser();
    await handOffToDesktopApp(CONVERSATION_ID);
    expect(openRequests(fetchMock)).toHaveLength(shouldHandOff ? 1 : 0);
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

  it.each([
    ["can't be reached", () => appOn(null)],
    ["couldn't bring the conversation up", () => appOn(DESKTOP_OPEN_PORTS[0], 504)],
  ])("says so when the user accepts the offer but the app %s", async (_label, appThen) => {
    browserPermission("granted");
    appOn(DESKTOP_OPEN_PORTS[0]);
    await handOffToDesktopApp(CONVERSATION_ID);

    appThen();
    offer()?.action.onClick();

    await vi.waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.stringContaining("Couldn't open")),
    );
    expect(handedOverConversationId()).toBeNull();
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
