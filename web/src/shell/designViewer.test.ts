import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchFileContent } from "@/hooks/useFileContent";
import { fetchWorkspaceDirectory } from "@/hooks/useWorkspaceChangedFiles";
import { getSessionSlim } from "@/lib/sessionsApi";
import { FakeBrandScanWorker } from "@/test/brandScanWorker";
import { listFixtureDir, readFixtureFile } from "@/test/designSystemFixture";
import { DESIGN_KIT_DIR, type KitFile } from "./codeViewerHelpers";
import { DESIGN_KIT_TIMEOUT_MS, useBrandWarnings, useDesignBranding } from "./designViewer";

vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({ getSessionSlim: vi.fn() }));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (orig) => ({
  ...(await orig<object>()),
  fetchWorkspaceDirectory: vi.fn(),
}));

const kitFile = (content: string): KitFile => ({
  encoding: "utf-8",
  content,
  bytes: content.length,
});

const VALID_KIT_JSON = JSON.stringify({
  name: "Acme",
  colors: { primary: "#ff0066", background: "#fafafa" },
});

const SYSTEM = "/Users/me/brand/fixture";
const deck = (color: string) =>
  `<html><head><style>h2{color:${color}}</style></head><body><section><h2>x</h2></section></body></html>`;

describe("useBrandWarnings", () => {
  beforeEach(() => {
    vi.stubGlobal("Worker", FakeBrandScanWorker);
    vi.mocked(fetchWorkspaceDirectory).mockImplementation(async (_id, dir) => {
      if (dir !== `${SYSTEM}/templates`) throw new Error("404 Not Found");
      return listFixtureDir("templates").map((e) => ({ ...e, path: `${dir}/${e.name}` })) as never;
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.mocked(fetchFileContent).mockReset();
    vi.mocked(fetchWorkspaceDirectory).mockReset();
  });

  it("retries a failed rules read after a content change", async () => {
    let adherenceReads = 0;
    vi.mocked(fetchFileContent).mockImplementation(async (_id, path) => {
      if (path === `${SYSTEM}/_adherence.oxlintrc.json`) {
        adherenceReads += 1;
        if (adherenceReads === 1) throw new Error("500 Server Error");
        const file = readFixtureFile("_adherence.oxlintrc.json");
        if (!file) throw new Error("404 Not Found");
        return { ...file, path } as never;
      }
      if (path.startsWith(`${SYSTEM}/`)) {
        const file = readFixtureFile(path.slice(SYSTEM.length + 1));
        if (file) return { ...file, path } as never;
      }
      throw new Error("404 Not Found");
    });

    const { result, rerender } = renderHook(
      ({ content }) => useBrandWarnings("conv_1", content, SYSTEM),
      { initialProps: { content: deck("#FF0000") } },
    );

    await waitFor(() => expect(adherenceReads).toBe(1));
    await waitFor(() => expect(result.current).toBeNull());

    rerender({ content: deck("#00AA00") });
    await waitFor(() => expect(adherenceReads).toBe(2));
    await waitFor(() => expect(result.current?.map((w) => w.value)).toEqual(["#00AA00"]));
  });
});

describe("useDesignBranding", () => {
  const html = "<html><body><section>a</section></body></html>";

  beforeEach(() => {
    vi.mocked(getSessionSlim).mockResolvedValue({ permissionLevel: null } as never);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.mocked(fetchFileContent).mockReset();
    vi.mocked(getSessionSlim).mockReset();
  });

  /** Serve kit.json after `gate`; everything else is a 404 (no design-system pointer). */
  function serveKitAfter(gate: Promise<void>, kitJson: string) {
    vi.mocked(fetchFileContent).mockImplementation(async (_id, path) => {
      if (path === `${DESIGN_KIT_DIR}/kit.json`) {
        await gate;
        return { ...kitFile(kitJson), path } as never;
      }
      throw new Error("404 Not Found");
    });
  }

  /** Flush microtasks under fake timers after releasing a gated read. */
  async function flushAfter(release: () => void) {
    release();
    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(0);
    });
  }

  it.each([
    ["with section rules (slides)", true],
    ["fonts/tokens only (wireframe)", false],
  ])("applies a kit that arrives after the timeout (%s)", async (_label, sections) => {
    vi.useFakeTimers();
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    serveKitAfter(gate, VALID_KIT_JSON);

    const { result } = renderHook(() => useDesignBranding("conv_1", html, sections));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DESIGN_KIT_TIMEOUT_MS + 1);
    });
    expect(result.current?.notice).toMatch(/design kit timed out/);
    expect(result.current?.badge).toBeNull();

    await flushAfter(release);
    expect(result.current?.badge).toEqual({ kind: "kit", name: "Acme" });
    expect(result.current?.notice).toBeNull();
    expect(result.current?.kitStyle).toContain("--kit-primary:#ff0066");
  });

  it("replaces a timed-out notice with the real reason when an invalid kit arrives late", async () => {
    vi.useFakeTimers();
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    serveKitAfter(gate, "{");

    const { result } = renderHook(() => useDesignBranding("conv_1", html));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DESIGN_KIT_TIMEOUT_MS + 1);
    });
    expect(result.current?.notice).toMatch(/design kit timed out/);

    await flushAfter(release);
    expect(result.current?.notice).toMatch(/kit\.json is not valid JSON/);
    expect(result.current?.notice).not.toMatch(/timed out/);
  });

  it("cancels a pending load on content change so a stale kit is not applied", async () => {
    vi.useFakeTimers();
    let releaseStale!: () => void;
    const staleGate = new Promise<void>((r) => {
      releaseStale = r;
    });
    let kitReads = 0;
    vi.mocked(fetchFileContent).mockImplementation(async (_id, path) => {
      if (path !== `${DESIGN_KIT_DIR}/kit.json`) throw new Error("404 Not Found");
      kitReads += 1;
      if (kitReads === 1) {
        await staleGate;
        return { ...kitFile(VALID_KIT_JSON), path } as never;
      }
      // Second content's load: no kit.
      throw new Error("404 Not Found");
    });

    const { result, rerender } = renderHook(({ content }) => useDesignBranding("conv_1", content), {
      initialProps: { content: `${html}<!--a-->` },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DESIGN_KIT_TIMEOUT_MS + 1);
    });
    expect(result.current?.notice).toMatch(/design kit timed out/);

    rerender({ content: `${html}<!--b-->` });
    // New load finds no kit and settles without a notice.
    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current?.badge).toBeNull();
    expect(result.current?.notice).toBeNull();

    // A late result for the superseded content must not re-apply the kit.
    await flushAfter(releaseStale);
    expect(result.current?.badge).toBeNull();
    expect(result.current?.kitStyle).toBe("");
    expect(result.current?.notice).toBeNull();
  });
});
