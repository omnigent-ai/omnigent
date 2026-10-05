import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fetchFileContent } from "@/hooks/useFileContent";
import { fetchWorkspaceDirectory } from "@/hooks/useWorkspaceChangedFiles";
import { FakeBrandScanWorker } from "@/test/brandScanWorker";
import { listFixtureDir, readFixtureFile } from "@/test/designSystemFixture";
import { useBrandWarnings } from "./designViewer";

vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (orig) => ({
  ...(await orig<object>()),
  fetchWorkspaceDirectory: vi.fn(),
}));

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
