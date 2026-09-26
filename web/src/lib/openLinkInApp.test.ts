// A chat link routed into the embedded browser must never be silently lost:
// the Browser tab surfaces only once the view accepted the URL, and a refused
// or failed bridge call reopens the link externally and tells the user why.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { showToast } from "@/components/ui/toast";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { maybeOpenLinkInApp, onInAppLinkOpen } from "@/lib/openLinkInApp";

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const CONVERSATION_ID = "conv-embedded-browser";
const LINK_URL = "https://example.com/page";
const VIEW_CAP_ERROR = "browser view cap reached — close one";

let openOrNavigate: ReturnType<typeof vi.fn>;
let openSpy: ReturnType<typeof vi.spyOn>;
let surfaced: string[];
let unsubscribe: () => void;

beforeEach(() => {
  openOrNavigate = vi.fn().mockResolvedValue({ ok: true, created: true });
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
    kind: "electron",
    setBadgeCount: () => {},
    notify: () => Promise.resolve(false),
    browserOpenOrNavigate: openOrNavigate,
  };
  openSpy = vi.spyOn(window, "open").mockReturnValue(null);
  surfaced = [];
  unsubscribe = onInAppLinkOpen((conversationId) => surfaced.push(conversationId));
  writeOpenLinksInApp(true);
});

afterEach(() => {
  unsubscribe();
  vi.restoreAllMocks();
  vi.mocked(showToast).mockReset();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  window.localStorage.clear();
});

describe("maybeOpenLinkInApp", () => {
  it("surfaces the Browser tab once the embedded view accepted the link", async () => {
    expect(maybeOpenLinkInApp(CONVERSATION_ID, LINK_URL)).toBe(true);
    expect(openOrNavigate).toHaveBeenCalledWith(CONVERSATION_ID, LINK_URL);
    // Not before the view answered: an empty pane must never surface first.
    expect(surfaced).toEqual([]);

    await vi.waitFor(() => expect(surfaced).toEqual([CONVERSATION_ID]));
    expect(openSpy).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
  });

  it("reopens the link externally with a toast when the view refuses it", async () => {
    openOrNavigate.mockResolvedValue({ ok: false, error: VIEW_CAP_ERROR });

    expect(maybeOpenLinkInApp(CONVERSATION_ID, LINK_URL)).toBe(true);

    await vi.waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(LINK_URL, "_blank", "noopener,noreferrer"),
    );
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(String(vi.mocked(showToast).mock.calls[0][0])).toContain(VIEW_CAP_ERROR);
    expect(surfaced).toEqual([]);
  });

  it("reopens the link externally with a toast when the bridge call rejects", async () => {
    openOrNavigate.mockRejectedValue(new Error("ipc channel closed"));
    vi.spyOn(console, "warn").mockImplementation(() => {});

    expect(maybeOpenLinkInApp(CONVERSATION_ID, LINK_URL)).toBe(true);

    await vi.waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(LINK_URL, "_blank", "noopener,noreferrer"),
    );
    expect(String(vi.mocked(showToast).mock.calls[0][0])).toContain("ipc channel closed");
    expect(surfaced).toEqual([]);
  });

  it("reopens the link externally when the bridge call throws synchronously", async () => {
    openOrNavigate.mockImplementation(() => {
      throw new Error("bridge unavailable");
    });
    vi.spyOn(console, "warn").mockImplementation(() => {});

    expect(maybeOpenLinkInApp(CONVERSATION_ID, LINK_URL)).toBe(true);

    await vi.waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(LINK_URL, "_blank", "noopener,noreferrer"),
    );
    expect(String(vi.mocked(showToast).mock.calls[0][0])).toContain("bridge unavailable");
    expect(surfaced).toEqual([]);
  });

  it("leaves the default path alone when the preference is off", () => {
    writeOpenLinksInApp(false);

    expect(maybeOpenLinkInApp(CONVERSATION_ID, LINK_URL)).toBe(false);
    expect(openOrNavigate).not.toHaveBeenCalled();
  });

  it.each([
    ["a non-web scheme", "mailto:docs@example.com"],
    ["an unparseable href", "http://[::1"],
  ])("leaves the default path alone for %s", (_label, href) => {
    expect(maybeOpenLinkInApp(CONVERSATION_ID, href)).toBe(false);
    expect(openOrNavigate).not.toHaveBeenCalled();
  });
});
