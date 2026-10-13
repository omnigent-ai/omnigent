import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { showToast } from "@/components/ui/toast";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { maybeOpenLinkInApp, onInAppLinkOpen } from "@/lib/openLinkInApp";

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const CONV = "conv-1";
const URL_ = "https://example.com/page";

let openOrNavigate: ReturnType<typeof vi.fn>;
let surfaced: string[];
let unsubscribe: () => void;

beforeEach(() => {
  openOrNavigate = vi.fn().mockResolvedValue({ ok: true });
  Object.assign(window, {
    omnigentDesktop: { kind: "electron", browserOpenOrNavigate: openOrNavigate },
  });
  vi.spyOn(window, "open").mockReturnValue(null);
  vi.spyOn(console, "warn").mockImplementation(() => {});
  surfaced = [];
  unsubscribe = onInAppLinkOpen((id) => surfaced.push(id));
  writeOpenLinksInApp(true);
});

afterEach(() => {
  unsubscribe();
  vi.restoreAllMocks();
  vi.mocked(showToast).mockReset();
  delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
  window.localStorage.clear();
});

it("surfaces the Browser tab only after the view accepts the link", async () => {
  expect(maybeOpenLinkInApp(CONV, URL_)).toBe(true);
  // Model-authored links get the agent navigation policy (no internal hosts).
  expect(openOrNavigate).toHaveBeenCalledWith(CONV, URL_, undefined, { agent: true });
  expect(surfaced).toEqual([]);
  await vi.waitFor(() => expect(surfaced).toEqual([CONV]));
  expect(window.open).not.toHaveBeenCalled();
});

it.each([
  ["refuses", () => openOrNavigate.mockResolvedValue({ ok: false, error: "view cap" }), "view cap"],
  ["rejects", () => openOrNavigate.mockRejectedValue(new Error("ipc closed")), "ipc closed"],
  [
    "throws",
    () =>
      openOrNavigate.mockImplementation(() => {
        throw new Error("no bridge");
      }),
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
});

it.each([
  ["the preference is off", CONV, URL_, () => writeOpenLinksInApp(false)],
  ["there is no conversation", undefined, URL_, () => {}],
  ["the scheme is not web", CONV, "mailto:a@example.com", () => {}],
  ["the href is unparseable", CONV, "http://[::1", () => {}],
])("leaves the default path alone when %s", (_label, conv, href, arrange) => {
  arrange();
  expect(maybeOpenLinkInApp(conv, href)).toBe(false);
  expect(openOrNavigate).not.toHaveBeenCalled();
});
