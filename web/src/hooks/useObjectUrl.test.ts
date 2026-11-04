import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useObjectUrl } from "./useObjectUrl";

const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;
let created = 0;
const urlsByBlob = new Map<Blob | MediaSource, string>();
const createObjectURL = vi.fn((blob: Blob | MediaSource) => {
  const url = `blob:test/${++created}`;
  urlsByBlob.set(blob, url);
  return url;
});
const revokeObjectURL = vi.fn();

beforeEach(() => {
  created = 0;
  urlsByBlob.clear();
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
});

afterEach(() => {
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
  vi.clearAllMocks();
});

/** Render the hook, recording which URL every render returned for which blob. */
function renderTracked(initial: Blob | null) {
  const renders: [Blob | null, string | undefined][] = [];
  const view = renderHook(
    ({ blob }) => {
      const url = useObjectUrl(blob);
      renders.push([blob, url]);
      return url;
    },
    { initialProps: { blob: initial } },
  );
  return { ...view, renders };
}

describe("useObjectUrl", () => {
  it("creates one URL per blob and revokes each exactly once", () => {
    const first = new Blob(["a"]);
    const { result, rerender, unmount } = renderTracked(first);
    expect(result.current).toBe("blob:test/1");

    rerender({ blob: first });
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).not.toHaveBeenCalled();

    rerender({ blob: new Blob(["b"]) });
    expect(result.current).toBe("blob:test/2");
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenLastCalledWith("blob:test/1");

    rerender({ blob: null });
    expect(result.current).toBeUndefined();
    expect(revokeObjectURL).toHaveBeenCalledTimes(2);
    expect(revokeObjectURL).toHaveBeenLastCalledWith("blob:test/2");

    rerender({ blob: new Blob(["c"]) });
    unmount();
    expect(createObjectURL).toHaveBeenCalledTimes(3);
    expect(revokeObjectURL).toHaveBeenCalledTimes(3);
    expect(revokeObjectURL).toHaveBeenLastCalledWith("blob:test/3");
  });

  it("never returns a URL that belongs to another blob", () => {
    const { rerender, renders } = renderTracked(new Blob(["a"]));
    rerender({ blob: new Blob(["b"]) });
    rerender({ blob: null });

    const mismatched = renders.filter(
      ([blob, url]) => url !== undefined && (blob === null || urlsByBlob.get(blob) !== url),
    );
    expect(mismatched).toEqual([]);
  });

  it("returns no URL where createObjectURL is missing", () => {
    // @ts-expect-error -- jsdom ships without createObjectURL.
    URL.createObjectURL = undefined;
    const { result } = renderHook(() => useObjectUrl(new Blob(["a"])));
    expect(result.current).toBeUndefined();
  });
});
