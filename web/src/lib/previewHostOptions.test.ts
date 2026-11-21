import { beforeEach, expect, it, vi } from "vitest";
import { previewHostOptions } from "./previewHostOptions";
import { fetchHosts } from "@/hooks/useHosts";
import { getDesktopFeatures } from "./nativeBridge";
import type * as NativeBridge from "./nativeBridge";

vi.mock("@/hooks/useHosts", () => ({ fetchHosts: vi.fn() }));
vi.mock("./nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  getDesktopFeatures: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getDesktopFeatures).mockResolvedValue({ sshLocalhostPreview: true });
  vi.mocked(fetchHosts).mockResolvedValue([
    { host_id: "remote", name: "devbox", status: "online" } as Awaited<
      ReturnType<typeof fetchHosts>
    >[number],
  ]);
});

it("adds an SSH route hint to a remote localhost navigation in a capable shell", async () => {
  expect(await previewHostOptions("remote", "http://dev.localhost:8787")).toEqual({
    sourceHostId: "remote",
    sessionHostProtocol: 1,
    sourceSshTarget: "devbox",
  });
});

it.each([null, {}, { sshLocalhostPreview: false }])(
  "keeps an older or absent shell on its existing path: %j",
  async (features) => {
    vi.mocked(getDesktopFeatures).mockResolvedValue(features);
    expect(await previewHostOptions("remote", "http://localhost:8787")).toEqual({
      sourceHostId: "remote",
      sessionHostProtocol: 1,
    });
    expect(fetchHosts).not.toHaveBeenCalled();
  },
);

it.each(["https://example.com", "http://127.0.0.2:8787", "file:///tmp/test", "bad URL"])(
  "does not discover SSH for %s",
  async (url) => {
    expect(await previewHostOptions("remote", url)).toEqual({
      sourceHostId: "remote",
      sessionHostProtocol: 1,
    });
    expect(fetchHosts).not.toHaveBeenCalled();
  },
);

it("keeps unknown provenance and discovery failures public-only", async () => {
  expect(await previewHostOptions(null, "http://localhost:8787")).toEqual({});
  vi.mocked(fetchHosts).mockRejectedValue(new Error("offline"));
  expect(await previewHostOptions("remote", "http://127.0.0.1:8787")).toEqual({
    sourceHostId: "remote",
    sessionHostProtocol: 1,
  });
});
