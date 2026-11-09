import { afterEach, describe, expect, it, vi } from "vitest";
import * as filesApi from "@/lib/filesApi";
import { recoverMessageAttachments } from "./recoverMessageAttachments";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("recoverMessageAttachments", () => {
  it("downloads uploads, decodes inline images, and names what it couldn't recover", async () => {
    const download = vi
      .spyOn(filesApi, "downloadSessionFile")
      .mockResolvedValue(new File(["notes"], "notes.txt", { type: "text/plain" }));

    const { files, missing } = await recoverMessageAttachments("conv_1", [
      { type: "input_text", text: "look" },
      { type: "input_file", file_id: "file_1", filename: "notes.txt" },
      // An imported transcript carries image bytes inline, with no stored file.
      { type: "input_image", image_url: "data:image/png;base64,iVBORw==", filename: "dot.png" },
      { type: "input_image", image_url: "data:image/gif;base64,R0lG" },
      { type: "input_file", file_id: "pending:upload.csv", filename: "upload.csv" },
      { type: "input_image", image_url: "https://example.com/remote.png", filename: "remote.png" },
    ]);

    expect(download).toHaveBeenCalledExactlyOnceWith("conv_1", "file_1", "notes.txt");
    expect(files.map((file) => [file.name, file.type])).toEqual([
      ["notes.txt", "text/plain"],
      ["dot.png", "image/png"],
      ["image.gif", "image/gif"],
    ]);
    expect([...new Uint8Array(await files[1]!.arrayBuffer())]).toEqual([0x89, 0x50, 0x4e, 0x47]);
    expect(missing).toEqual(["upload.csv", "remote.png"]);
  });

  it("keeps what it recovered when other attachments fail", async () => {
    vi.spyOn(filesApi, "downloadSessionFile").mockImplementation(async (_session, fileId, name) => {
      if (fileId === "file_ok") return new File(["ok"], name);
      throw new Error("404");
    });

    const { files, missing } = await recoverMessageAttachments("conv_1", [
      { type: "input_file", file_id: "file_gone", filename: "gone.txt" },
      { type: "input_file", file_id: "file_ok", filename: "ok.txt" },
      { type: "input_image", image_url: "data:image/png;base64,%%%", filename: "broken.png" },
      // A non-string filename from an imported transcript is labelled by its file id.
      { type: "input_file", file_id: "file_odd", filename: 42 as unknown as string },
      { type: "input_file", file_id: "pending:draft.csv" },
    ]);

    expect(files.map((file) => file.name)).toEqual(["ok.txt"]);
    expect(missing).toEqual(["gone.txt", "broken.png", "file_odd", "draft.csv"]);
  });

  it("decodes percent-encoded data URIs byte for byte", async () => {
    const { files, missing } = await recoverMessageAttachments(null, [
      // "iVBORw==" with its "=" padding percent-encoded.
      { type: "input_image", image_url: "data:image/png;base64,iVBORw%3D%3D", filename: "a.png" },
      { type: "input_image", image_url: "data:image/png,%89PNG%0D", filename: "b.png" },
    ]);

    expect(missing).toEqual([]);
    expect([...new Uint8Array(await files[0]!.arrayBuffer())]).toEqual([0x89, 0x50, 0x4e, 0x47]);
    expect([...new Uint8Array(await files[1]!.arrayBuffer())]).toEqual([
      0x89, 0x50, 0x4e, 0x47, 0x0d,
    ]);
  });

  it("can't download without the session it was sent in", async () => {
    const download = vi.spyOn(filesApi, "downloadSessionFile");
    const result = await recoverMessageAttachments(null, [
      { type: "input_image", file_id: "img_1", filename: "a.png" },
    ]);
    expect(download).not.toHaveBeenCalled();
    expect(result).toEqual({ files: [], missing: ["a.png"] });
  });
});
