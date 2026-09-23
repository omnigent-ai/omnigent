import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("./sessionsApi", () => ({ apiErrorFromResponse: vi.fn() }));

import { authenticatedFetch } from "./identity";
import { uploadFile } from "./filesApi";

afterEach(() => vi.resetAllMocks());

describe("uploadFile filenames", () => {
  it.each([
    ["screenshot.png", "screenshot.png"],
    ["", "image.png"],
  ])("uploads %j as %j without renaming the original File", async (name, expected) => {
    const file = new File([new Uint8Array(4)], name, { type: "image/png" });
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ id: "file_1" }), { status: 201 }),
    );

    const uploaded = await uploadFile("session/1", file);

    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/session%2F1/resources/files",
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );
    const form = vi.mocked(authenticatedFetch).mock.calls[0]![1]!.body as FormData;
    expect((form.get("file") as File).name).toBe(expected);
    expect(uploaded.filename).toBe(expected);
    expect(file.name).toBe(name);
  });

  it("prefers the server's authoritative filename", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "file_1",
          name: "resource-name",
          metadata: { filename: "stored.png" },
        }),
        { status: 201 },
      ),
    );
    const uploaded = await uploadFile("session_1", new File([], "", { type: "image/png" }));
    expect(uploaded.filename).toBe("stored.png");
  });
});
