import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import {
  acknowledgedOffset,
  getUploadCapability,
  uploadRequest,
  uploadUrl,
  UploadError,
} from "./workspaceUploadsApi";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));
const owner = {
  origin: "http://server",
  identity: "alice",
  session: "a/b",
  environment: "default",
};
afterEach(() => vi.resetAllMocks());

it("routes all upload operations through authenticatedFetch, preserving raw bodies and abort signals", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(null, { status: 204 }));
  const init = { method: "PATCH", body: new Blob(["raw"]), signal: new AbortController().signal };
  await uploadRequest(owner, "/id", init);
  expect(authenticatedFetch).toHaveBeenCalledWith(
    "/v1/sessions/a%2Fb/resources/environments/default/uploads/id",
    init,
  );
  expect(uploadUrl(owner)).toContain("a%2Fb");
});

it("hides flag-off 404 capabilities and preserves typed failure headers", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response(null, { status: 404 }));
  expect(await getUploadCapability(owner)).toBeNull();
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(
    new Response(JSON.stringify({ error: { code: "offset_mismatch", message: "resync" } }), {
      status: 409,
      headers: { "Upload-Offset": "3" },
    }),
  );
  await expect(uploadRequest(owner, "/id")).rejects.toMatchObject({
    status: 409,
    code: "offset_mismatch",
    message: "resync",
  });
});

it.each([null, "bad", "-1", "21", "9007199254740993"])(
  "rejects invalid acknowledged offset %s",
  (value) => {
    const error = new UploadError(
      "offset",
      409,
      "offset_mismatch",
      new Headers(value ? { "Upload-Offset": value } : {}),
    );
    expect(() => acknowledgedOffset(error, 20)).toThrow("Invalid upload acknowledgement");
  },
);

it("uses server acknowledgement, including zero", () => {
  expect(acknowledgedOffset(new Response(null, { headers: { "Upload-Offset": "0" } }), 20)).toBe(0);
  expect(acknowledgedOffset(new Response(null, { headers: { "Upload-Offset": "7" } }), 20)).toBe(7);
});
