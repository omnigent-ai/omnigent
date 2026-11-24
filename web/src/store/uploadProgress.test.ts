import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  beginUploadProgress,
  endUploadProgress,
  reportUploadProgress,
  uploadProgressKey,
  useUploadProgressStore,
} from "./uploadProgress";

beforeEach(() => {
  useUploadProgressStore.setState({ uploads: {} });
});

describe("upload progress store", () => {
  it("tracks each upload by key from its start to its end", () => {
    const cancel = vi.fn();
    beginUploadProgress("msg/file", { name: "a.zip", size: 100, cancel });

    expect(useUploadProgressStore.getState().uploads["msg/file"]).toEqual({
      name: "a.zip",
      size: 100,
      loaded: 0,
      total: null,
      cancel,
    });

    reportUploadProgress("msg/file", 50, 100);
    expect(useUploadProgressStore.getState().uploads["msg/file"]).toMatchObject({
      loaded: 50,
      total: 100,
    });

    endUploadProgress("msg/file");
    expect(useUploadProgressStore.getState().uploads).toEqual({});
  });

  it("keeps the last known total when a later report omits it", () => {
    beginUploadProgress("k", { name: "a", size: 10 });
    reportUploadProgress("k", 4, 10);
    reportUploadProgress("k", 6, null);
    expect(useUploadProgressStore.getState().uploads.k).toMatchObject({ loaded: 6, total: 10 });
  });

  it("ignores reports for an upload that has ended", () => {
    beginUploadProgress("k", { name: "a", size: 10 });
    endUploadProgress("k");
    reportUploadProgress("k", 5, 10);
    expect(useUploadProgressStore.getState().uploads).toEqual({});
  });

  it("re-renders only on a whole-percent step, or at the end", () => {
    beginUploadProgress("k", { name: "a", size: 1000 });
    reportUploadProgress("k", 100, 1000);
    const afterFirstStep = useUploadProgressStore.getState().uploads;
    reportUploadProgress("k", 105, 1000);
    expect(useUploadProgressStore.getState().uploads).toBe(afterFirstStep);
    reportUploadProgress("k", 1000, 1000);
    expect(useUploadProgressStore.getState().uploads.k).toMatchObject({ loaded: 1000 });
  });

  it("keys an attachment by its message and its pending file id", () => {
    expect(uploadProgressKey("pend_1", "pending:a.zip")).toBe("pend_1/pending:a.zip");
  });
});
