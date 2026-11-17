// Where dictation loads its AudioWorklet from, with and without the server's
// Content-Security-Policy. The probe cache is module state, so each case
// re-imports the modules.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal(
    "URL",
    class extends URL {
      static override createObjectURL = vi.fn(() => "blob:worklet");
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

async function workletUrlFor(contentSecurityPolicy: string): Promise<string> {
  fetchMock.mockResolvedValueOnce({
    ok: true,
    json: async () => ({ data_controls: { content_security_policy: contentSecurityPolicy } }),
  });
  const { resolveServerInfo } = await import("./capabilities");
  await resolveServerInfo();
  const { workletUrl } = await import("./dictation");
  return workletUrl();
}

describe("dictation worklet", () => {
  it("loads from a Blob by default", async () => {
    expect(await workletUrlFor("off")).toBe("blob:worklet");
  });

  it.each(["enforce", "report-only"])(
    "loads the built file under a %s policy, which refuses blob: scripts",
    async (mode) => {
      expect(await workletUrlFor(mode)).toMatch(/pcm16Worklet\.js/);
    },
  );
});
