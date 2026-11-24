import { afterEach, beforeEach, expect, it, vi } from "vitest";

const URL = "https://docs.google.com/document/d/example/edit";
const entry = { documentUrl: URL, addedAt: 1 };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { resolve, promise };
}

beforeEach(() => {
  vi.resetModules();
  window.__OMNIGENT_BASE_PATH__ = "";
});
afterEach(() => {
  vi.unstubAllEnvs();
});

it.each(["add", "remove"])(
  "blocks %s dispatch after a backend switch during real authenticated host resolution",
  async (operation) => {
    const host = await import("./host");
    const identity = await import("./identity");
    const documents = await import("./sessionDocuments");
    const routing = deferred<void>();
    const resolving = deferred<void>();
    const oldFetcher = vi.fn<typeof fetch>();
    const newFetcher = vi.fn<typeof fetch>();
    host.setOmnigentHostConfig({ serverIdentity: "backend-a", fetcher: oldFetcher });
    identity.setSessionHostResolver(async () => {
      resolving.resolve();
      await routing.promise;
    });
    const request =
      operation === "add"
        ? documents.addSessionDocument("session-a", URL)
        : documents.removeSessionDocument("session-a", entry);
    const failed = expect(request.result).rejects.toThrow("changed");
    await resolving.promise;
    host.setOmnigentHostConfig({ serverIdentity: "backend-b", fetcher: newFetcher });
    routing.resolve();
    await failed;
    await expect(request.retry().result).rejects.toThrow("changed");
    expect(oldFetcher).not.toHaveBeenCalled();
    expect(newFetcher).not.toHaveBeenCalled();
  },
);

it.each(["add", "remove"])(
  "blocks %s replica resends after a backend switch while decoding the refusal",
  async (operation) => {
    const host = await import("./host");
    const identity = await import("./identity");
    const { setSessionHost } = await import("./sessionHost");
    const documents = await import("./sessionDocuments");
    const refusal = deferred<unknown>();
    const decoding = deferred<void>();
    const oldFetcher = vi.fn<typeof fetch>().mockResolvedValue({
      status: 400,
      ok: false,
      clone: () => ({
        json: () => {
          decoding.resolve();
          return refusal.promise;
        },
      }),
    } as Response);
    const newFetcher = vi.fn<typeof fetch>();
    host.setOmnigentHostConfig({ serverIdentity: "backend-a", fetcher: oldFetcher });
    setSessionHost("session-a", "host-a");
    identity.setSessionHostResolver(null);
    const request =
      operation === "add"
        ? documents.addSessionDocument("session-a", URL)
        : documents.removeSessionDocument("session-a", entry);
    const failed = expect(request.result).rejects.toThrow("changed");
    await decoding.promise;
    host.setOmnigentHostConfig({ serverIdentity: "backend-b", fetcher: newFetcher });
    refusal.resolve({ error: { code: "wrong_replica", slice_key: "host-b" } });
    await failed;
    await expect(request.retry().result).rejects.toThrow("changed");
    expect(oldFetcher).toHaveBeenCalledOnce();
    expect(newFetcher).not.toHaveBeenCalled();
  },
);

it("uses the real authenticated transport to commit the shared collection on a stable backend", async () => {
  const host = await import("./host");
  const identity = await import("./identity");
  const documents = await import("./sessionDocuments");
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
    Response.json({
      session_id: "session-a",
      entries: [{ document_url: URL, added_at: 1 }],
    }),
  );
  host.setOmnigentHostConfig({ serverIdentity: "backend-a", fetcher });
  identity.setSessionHostResolver(async () => {});
  const request = documents.addSessionDocument("session-a", URL);
  expect((await request.result).entries).toEqual([entry]);
  expect(request.isCurrent("session-a")).toBe(true);
  expect(fetcher).toHaveBeenCalledOnce();
  expect(fetcher.mock.calls[0][1]?.method).toBe("POST");
});
