import { render } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { ownerKey, uploadTotals, useUploadStore, type UploadRow } from "./uploadStore";

export function testUploadRow(overrides: Partial<UploadRow> = {}): UploadRow {
  const owner = {
    origin: "http://server",
    identity: "alice",
    session: "s",
    environment: "default",
  };
  return {
    ...owner,
    key: `${ownerKey(owner)}:id`,
    createKey: "a".repeat(32),
    id: "id",
    batch: "batch",
    file: new File([new Uint8Array(20)], "a.bin"),
    name: "a.bin",
    size: 20,
    directory: "src",
    destination: "src",
    offset: 0,
    controller: new AbortController(),
    status: "queued",
    conflict: "fail",
    limits: {
      chunk: { min: 2, initial: 4, max: 8 },
      max_file_bytes: 100,
      max_files_per_batch: 10,
      max_batch_bytes: 200,
      max_open_uploads: 16,
      max_open_bytes: 200,
      parallel_files: 2,
    },
    ...overrides,
  };
}
afterEach(() => useUploadStore.setState({ rows: {}, announcement: "" }));

it("keeps the File handle and frozen destination across a subscriber unmount and session switch", () => {
  const row = testUploadRow();
  useUploadStore.getState().add([row]);
  function Subscriber() {
    const rows = useUploadStore((state) => state.rows);
    return <span>{Object.keys(rows).length}</span>;
  }
  const view = render(<Subscriber />);
  view.unmount();
  expect(useUploadStore.getState().rows[row.key].file).toBe(row.file);
  expect(useUploadStore.getState().rows[row.key].directory).toBe("src");
  expect(ownerKey({ ...row, session: "other" })).not.toBe(ownerKey(row));
});

it("aborts and drops only the departing identity's handles", () => {
  const row = testUploadRow();
  const other = testUploadRow({ key: "other", identity: "bob" });
  useUploadStore.getState().add([row, other]);
  useUploadStore.getState().discardOwner(row.origin, row.identity);
  expect(row.controller.signal.aborted).toBe(true);
  expect(useUploadStore.getState().rows[row.key]).toBeUndefined();
  expect(useUploadStore.getState().rows.other.file).toBe(other.file);
});

it("weights progress by bytes and protects active rows from dismissal", () => {
  const row = testUploadRow({ status: "uploading", offset: 10 });
  const done = testUploadRow({ key: "done", status: "done", size: 80, offset: 80 });
  useUploadStore.getState().add([row, done]);
  expect(uploadTotals([row, done])).toEqual({
    total: 100,
    acknowledged: 90,
    committed: 80,
    done: 1,
    active: true,
  });
  useUploadStore.getState().dismiss([row.key, done.key]);
  expect(Object.keys(useUploadStore.getState().rows)).toEqual([row.key]);
});

it("protects cancelled rows from dismissal until reconciliation settles", () => {
  const row = testUploadRow({ status: "cancelled", file: undefined, cancellationPending: true });
  useUploadStore.getState().add([row]);
  expect(uploadTotals([row]).active).toBe(true);
  useUploadStore.getState().dismiss([row.key]);
  expect(useUploadStore.getState().rows[row.key]).toBeDefined();
  useUploadStore.getState().update(row.key, { cancellationPending: false });
  useUploadStore.getState().dismiss([row.key]);
  expect(useUploadStore.getState().rows[row.key]).toBeUndefined();
});
