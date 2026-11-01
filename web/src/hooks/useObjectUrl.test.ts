import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useObjectUrl } from "./useObjectUrl";

const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;
let created = 0;
const createObjectURL = vi.fn((_blob: Blob | MediaSource) => `blob:test/${++created}`);
const revokeObjectURL = vi.fn();

beforeEach(() => {
  created = 0;
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
});

afterEach(() => {
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
  vi.clearAllMocks();
});

describe("useObjectUrl", () => {
  it("revokes each URL once its blob is replaced, cleared or unmounted", () => {
    const first = new Blob(["a"]);
    const { result, rerender, unmount } = renderHook(({ blob }) => useObjectUrl(blob), {
      initialProps: { blob: first as Blob | null },
    });
    expect(result.current).toBe("blob:test/1");
    expect(createObjectURL).toHaveBeenCalledWith(first);

    rerender({ blob: new Blob(["b"]) });
    expect(revokeObjectURL).toHaveBeenLastCalledWith("blob:test/1");
    expect(result.current).toBe("blob:test/2");

    rerender({ blob: null });
    expect(revokeObjectURL).toHaveBeenLastCalledWith("blob:test/2");
    expect(result.current).toBeUndefined();

    rerender({ blob: new Blob(["c"]) });
    unmount();
    expect(revokeObjectURL).toHaveBeenLastCalledWith("blob:test/3");
  });

  it("returns no URL where createObjectURL is missing", () => {
    // @ts-expect-error -- jsdom ships without createObjectURL.
    URL.createObjectURL = undefined;
    const { result } = renderHook(() => useObjectUrl(new Blob(["a"])));
    expect(result.current).toBeUndefined();
  });
});
