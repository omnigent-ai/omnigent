import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { showToast } from "@/components/ui/toast";
import { AGENT_BROWSER_TAB_ID, browserViewId } from "@/lib/browserTabs";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { maybeOpenLinkInApp, onInAppLinkOpen, parseHttpUrl } from "@/lib/openLinkInApp";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const getSessionSlim = vi.fn();
vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: (...args: unknown[]) => getSessionSlim(...args),
}));

const CONV = "conv-1";
const URL_ = "https://example.com/page";

let openOrNavigate: ReturnType<typeof vi.fn>;
let hasView: ReturnType<typeof vi.fn>;
let closeView: ReturnType<typeof vi.fn>;
let surfaced: { conversationId: string; tabId: string }[];
let unsubscribe: () => void;

beforeEach(() => {
  getSessionSlim.mockReset().mockImplementation(async (id: string) => ({
    id,
    hostId: "local-host",
    parentSessionId: null,
  }));
  openOrNavigate = vi.fn().mockResolvedValue({ ok: true });
  hasView = vi.fn().mockResolvedValue({ exists: false });
  closeView = vi.fn().mockResolvedValue({ ok: true });
  Object.assign(window, {
    omnigentDesktop: {
      kind: "electron",
      browserOpenOrNavigate: openOrNavigate,
      browserHasView: hasView,
      browserClose: closeView,
    },
  });
  vi.spyOn(window, "open").mockReturnValue(null);
  vi.spyOn(console, "warn").mockImplementation(() => {});
  surfaced = [];
  unsubscribe = onInAppLinkOpen((conversationId, tabId) =>
    surfaced.push({ conversationId, tabId }),
  );
  writeOpenLinksInApp(true);
});

afterEach(() => {
  unsubscribe();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.mocked(showToast).mockReset();
  delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
  window.localStorage.clear();
});

it.each([true, false])(
  "opens distinct links in their own tabs only after the view accepts them (native UUID: %s)",
  async (nativeUUIDAvailable) => {
    if (!nativeUUIDAvailable) {
      vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    }
    expect(maybeOpenLinkInApp(CONV, URL_)).toBe(true);
    expect(surfaced).toEqual([]);
    await vi.waitFor(() => expect(openOrNavigate).toHaveBeenCalledOnce());
    const firstViewId = openOrNavigate.mock.calls[0][0];
    expect(firstViewId).toMatch(/^browser-tab:conv-1:[^:]+$/);
    expect(openOrNavigate).toHaveBeenCalledWith(firstViewId, URL_, undefined, {
      agent: true,
      sourceHostId: "local-host",
      sessionHostProtocol: 1,
    });
    await vi.waitFor(() =>
      expect(surfaced).toEqual([{ conversationId: CONV, tabId: firstViewId.split(":")[2] }]),
    );
    expect(maybeOpenLinkInApp(CONV, "https://example.com/other")).toBe(true);
    await vi.waitFor(() => expect(openOrNavigate).toHaveBeenCalledTimes(2));
    const secondViewId = openOrNavigate.mock.calls[1][0];
    expect(secondViewId).not.toBe(firstViewId);
    await vi.waitFor(() => expect(surfaced).toHaveLength(2));
    expect(surfaced[1]).toEqual({ conversationId: CONV, tabId: secondViewId.split(":")[2] });
    expect(window.open).not.toHaveBeenCalled();
    expect(closeView).not.toHaveBeenCalled();
  },
);

it("selects an existing tab for the same normalized URL without reloading it", async () => {
  writeSessionWorkspaceState(CONV, { openBrowsers: ["existing"] });
  hasView.mockResolvedValue({ exists: true, url: URL_ });

  expect(maybeOpenLinkInApp(CONV, "https://EXAMPLE.com:443/page")).toBe(true);
  await vi.waitFor(() => expect(surfaced).toEqual([{ conversationId: CONV, tabId: "existing" }]));
  expect(hasView).toHaveBeenCalledWith(browserViewId(CONV, "existing"));
  expect(openOrNavigate).not.toHaveBeenCalled();
  expect(window.open).not.toHaveBeenCalled();
  expect(closeView).not.toHaveBeenCalled();
});

it("reuses a tab whose initial navigation is still loading", async () => {
  writeSessionWorkspaceState(CONV, { openBrowsers: ["loading"] });
  hasView.mockResolvedValue({ exists: true, url: "about:blank", pendingUrl: URL_ });

  maybeOpenLinkInApp(CONV, URL_);
  await vi.waitFor(() => expect(surfaced).toEqual([{ conversationId: CONV, tabId: "loading" }]));
  expect(openOrNavigate).not.toHaveBeenCalled();
});

it.each([
  ["closed", { exists: false }],
  ["navigated elsewhere", { exists: true, url: "https://example.com/other" }],
  ["different query", { exists: true, url: `${URL_}?page=2` }],
  ["different fragment", { exists: true, url: `${URL_}#section` }],
  ["idle blank", { exists: true, url: "about:blank" }],
])("opens a fresh tab when the existing tab is %s", async (_label, view) => {
  writeSessionWorkspaceState(CONV, { openBrowsers: ["old"] });
  hasView.mockResolvedValue(view);

  maybeOpenLinkInApp(CONV, URL_);
  await vi.waitFor(() => expect(surfaced).toHaveLength(1));
  expect(surfaced[0].tabId).not.toBe("old");
  expect(openOrNavigate).toHaveBeenCalledOnce();
});

it("coalesces repeated clicks before the first navigation is accepted", async () => {
  let accept!: (result: { ok: boolean }) => void;
  openOrNavigate.mockReturnValue(
    new Promise((resolve) => {
      accept = resolve;
    }),
  );

  maybeOpenLinkInApp(CONV, URL_);
  maybeOpenLinkInApp(CONV, URL_);
  await vi.waitFor(() => expect(openOrNavigate).toHaveBeenCalledOnce());
  accept({ ok: true });
  await vi.waitFor(() => expect(surfaced).toHaveLength(1));
});

it("keeps conversations independent and leaves the reserved agent tab alone", async () => {
  writeSessionWorkspaceState(CONV, { openBrowsers: [AGENT_BROWSER_TAB_ID] });
  writeSessionWorkspaceState("other-conversation", { openBrowsers: ["other-tab"] });
  hasView.mockResolvedValue({ exists: true, url: URL_ });

  maybeOpenLinkInApp(CONV, URL_);
  await vi.waitFor(() => expect(surfaced).toHaveLength(1));
  expect(hasView).not.toHaveBeenCalled();
  expect(surfaced[0].tabId).not.toBe(AGENT_BROWSER_TAB_ID);
  expect(surfaced[0].tabId).not.toBe("other-tab");
});

it("still finds a matching tab when a different view probe fails", async () => {
  writeSessionWorkspaceState(CONV, { openBrowsers: ["unavailable", "matching"] });
  hasView.mockImplementation(async (viewId: string) => {
    if (viewId === browserViewId(CONV, "unavailable")) throw new Error("view closed");
    return { exists: true, url: URL_ };
  });

  maybeOpenLinkInApp(CONV, URL_);
  await vi.waitFor(() => expect(surfaced).toEqual([{ conversationId: CONV, tabId: "matching" }]));
  expect(openOrNavigate).not.toHaveBeenCalled();
  expect(window.open).not.toHaveBeenCalled();
});

it("does not restore a tab removed while its view probe was pending", async () => {
  writeSessionWorkspaceState(CONV, { openBrowsers: ["closing"] });
  let finishProbe!: (result: { exists: boolean; url: string }) => void;
  hasView.mockReturnValue(
    new Promise((resolve) => {
      finishProbe = resolve;
    }),
  );

  maybeOpenLinkInApp(CONV, URL_);
  writeSessionWorkspaceState(CONV, { openBrowsers: [] });
  finishProbe({ exists: true, url: URL_ });
  await vi.waitFor(() => expect(surfaced).toHaveLength(1));
  expect(surfaced[0].tabId).not.toBe("closing");
  expect(openOrNavigate).toHaveBeenCalledOnce();
});

it("can still open links in an older shell without view-probe or cleanup methods", async () => {
  writeSessionWorkspaceState(CONV, { openBrowsers: ["existing"] });
  Object.assign(window, {
    omnigentDesktop: { kind: "electron", browserOpenOrNavigate: openOrNavigate },
  });

  maybeOpenLinkInApp(CONV, URL_);
  await vi.waitFor(() => expect(surfaced).toHaveLength(1));
  expect(openOrNavigate).toHaveBeenCalledOnce();

  openOrNavigate.mockResolvedValue({ ok: false, error: "navigation rejected" });
  const failedUrl = "https://example.com/failure";
  maybeOpenLinkInApp(CONV, failedUrl);
  await vi.waitFor(() =>
    expect(window.open).toHaveBeenCalledWith(failedUrl, "_blank", "noopener,noreferrer"),
  );
  expect(surfaced).toHaveLength(1);
  expect(closeView).not.toHaveBeenCalled();
});

it.each([
  ["refuses", () => openOrNavigate.mockResolvedValue({ ok: false, error: "view cap" }), "view cap"],
  [
    "rejects, including cleanup",
    () => {
      closeView.mockRejectedValue(new Error("cleanup rejected"));
      openOrNavigate.mockRejectedValue(new Error("ipc closed"));
    },
    "ipc closed",
  ],
  [
    "throws, including cleanup",
    () => {
      closeView.mockImplementation(() => {
        throw new Error("cleanup threw");
      });
      openOrNavigate.mockImplementation(() => {
        throw new Error("no bridge");
      });
    },
    "no bridge",
  ],
])("reopens externally with a toast when the bridge %s", async (_label, arrange, reason) => {
  arrange();
  expect(maybeOpenLinkInApp(CONV, URL_)).toBe(true);
  await vi.waitFor(() =>
    expect(window.open).toHaveBeenCalledWith(URL_, "_blank", "noopener,noreferrer"),
  );
  expect(String(vi.mocked(showToast).mock.calls[0][0])).toContain(reason);
  expect(surfaced).toEqual([]);
  expect(closeView).toHaveBeenCalledExactlyOnceWith(openOrNavigate.mock.calls[0][0]);
});

it.each([
  ["the preference is off", CONV, URL_, () => writeOpenLinksInApp(false)],
  [
    "the shell has no browser bridge",
    CONV,
    URL_,
    () => Reflect.deleteProperty(window, "omnigentDesktop"),
  ],
  ["there is no conversation", undefined, URL_, () => {}],
  ["the scheme is not web", CONV, "mailto:a@example.com", () => {}],
  ["the href is unparseable", CONV, "http://[::1", () => {}],
])("leaves the default path alone when %s", (_label, conv, href, arrange) => {
  arrange();
  expect(maybeOpenLinkInApp(conv, href)).toBe(false);
  expect(openOrNavigate).not.toHaveBeenCalled();
});

it.each([
  ["https://example.com/page", "https://example.com/page"],
  ["http://localhost:3000/x", "http://localhost:3000/x"],
  ["javascript:alert(1)", null],
  ["  JavaScript:alert(1)", null],
  ["data:text/html,<script>alert(1)</script>", null],
  ["http://[", null],
])("parseHttpUrl(%j) keeps only http(s) URLs", (href, expected) => {
  expect(parseHttpUrl(href)?.href ?? null).toBe(expected);
});

it("omits host options when the owning session has no resolvable host", async () => {
  getSessionSlim.mockRejectedValue(new Error("host unavailable"));
  maybeOpenLinkInApp(CONV, URL_);
  await vi.waitFor(() =>
    expect(openOrNavigate).toHaveBeenCalledWith(expect.any(String), URL_, undefined, {
      agent: true,
    }),
  );
  expect(getSessionSlim).toHaveBeenCalledWith(CONV);
});

it("resolves the current host for a new link after the session moves", async () => {
  getSessionSlim
    .mockResolvedValueOnce({ id: CONV, hostId: "local-host", parentSessionId: null })
    .mockResolvedValue({ id: CONV, hostId: "remote-host", parentSessionId: null });
  maybeOpenLinkInApp(CONV, "http://127.0.0.1:5173/before");
  await vi.waitFor(() => expect(surfaced).toHaveLength(1));
  maybeOpenLinkInApp(CONV, "http://127.0.0.1:5173/after");
  await vi.waitFor(() =>
    expect(openOrNavigate).toHaveBeenLastCalledWith(
      expect.any(String),
      "http://127.0.0.1:5173/after",
      undefined,
      { agent: true, sourceHostId: "remote-host", sessionHostProtocol: 1 },
    ),
  );
  expect(getSessionSlim).toHaveBeenCalledTimes(2);
});
