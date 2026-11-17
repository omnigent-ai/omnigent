import { create, fromBinary, toBinary, type MessageInitShape } from "@bufbuild/protobuf";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ContentEncoding,
  EntryType,
  ListDirectoryRequestSchema,
  ListDirectoryResponseSchema,
  ReadFileResponseSchema,
} from "@/gen/omnigent/api/workspace/v1alpha1/workspace_pb";
import { getCachedServerInfo, type ServerInfo } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { workspaceSocketBridge, type WorkspaceSocketResponse } from "@/lib/nativeBridge";
import { listDirectoryViaWorkspaceApi, readFileViaWorkspaceApi } from "./workspaceApi";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/nativeBridge", () => ({ workspaceSocketBridge: vi.fn() }));
vi.mock(import("@/lib/capabilities"), async (original) => ({
  ...(await original()),
  getCachedServerInfo: vi.fn(),
}));

const READ_FILE = "omnigent.api.workspace.v1alpha1.WorkspaceService/ReadFile";
type ReadFileMessage = MessageInitShape<typeof ReadFileResponseSchema>;

function envelope(flags: number, payload: Uint8Array): Uint8Array {
  const frame = new Uint8Array(5 + payload.length);
  frame[0] = flags;
  new DataView(frame.buffer).setUint32(1, payload.length);
  frame.set(payload, 5);
  return frame;
}

/** A Connect server-streaming response body: messages, then the end of the stream. */
function stream(messages: ReadFileMessage[], end: object = {}): Uint8Array<ArrayBuffer> {
  const frames = [
    ...messages.map((m) =>
      envelope(0, toBinary(ReadFileResponseSchema, create(ReadFileResponseSchema, m))),
    ),
    envelope(2, new TextEncoder().encode(JSON.stringify(end))),
  ];
  const body = new Uint8Array(frames.reduce((n, f) => n + f.length, 0));
  frames.reduce((offset, f) => (body.set(f, offset), offset + f.length), 0);
  return body;
}

const CONNECT_HEADERS = { "content-type": "application/connect+proto" };
const textFile = (): Uint8Array<ArrayBuffer> =>
  stream([
    { metadata: { size: 6n, encoding: ContentEncoding.UTF8, contentType: "text/plain" } },
    // "héllo", split inside the "é".
    { data: new Uint8Array([0x68, 0xc3]) },
    { data: new Uint8Array([0xa9, 0x6c, 0x6c, 0x6f]) },
  ]);

function enable(...features: string[]): void {
  vi.mocked(getCachedServerInfo).mockReturnValue({
    features: Object.fromEntries(features.map((f) => [f, true])),
  } as unknown as ServerInfo);
}

/** Answer the server's grant and API calls like a server with the given ReadFile body. */
function serve(readFile: () => Uint8Array<ArrayBuffer>): void {
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    if (String(input).endsWith("/direct-access")) {
      return Response.json({
        host_id: "host-1",
        socket_path: "/tmp/omnigent-workspace-x/workspace.sock",
        token: "tok",
        expires_at: Date.now() / 1000 + 300,
      });
    }
    return new Response(readFile(), { headers: CONNECT_HEADERS });
  });
}

const socket = vi.fn<(request: unknown) => Promise<WorkspaceSocketResponse>>();
const fromSocket = (body: Uint8Array<ArrayBuffer>, status = 200): WorkspaceSocketResponse => ({
  status,
  headers: status === 200 ? CONNECT_HEADERS : { "content-type": "application/json" },
  body,
});
const grantRequests = () =>
  vi
    .mocked(authenticatedFetch)
    .mock.calls.filter(([input]) => String(input).endsWith("/direct-access")).length;

beforeEach(() => {
  vi.mocked(workspaceSocketBridge).mockReturnValue(socket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe("readFileViaWorkspaceApi", () => {
  it("leaves the read to REST when no route is enabled", async () => {
    enable();

    expect(await readFileViaWorkspaceApi("s-off", "notes.md")).toBeNull();
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(socket).not.toHaveBeenCalled();
  });

  it("returns the REST payload for text and binary files through the server", async () => {
    enable("workspace_api_server_access");
    serve(textFile);

    const text = await readFileViaWorkspaceApi("s-server", "notes.md");

    expect(text).toEqual({
      object: "session.environment.filesystem.file_content",
      path: "notes.md",
      content_type: "text/plain",
      encoding: "utf-8",
      content: "héllo",
      bytes: 6,
      truncated: false,
    });
    expect(String(vi.mocked(authenticatedFetch).mock.calls[0]![0])).toBe(
      `/v1/sessions/s-server/workspace/${READ_FILE}`,
    );

    serve(() =>
      stream([
        { metadata: { size: 2n, encoding: ContentEncoding.BINARY, truncated: true } },
        { data: new Uint8Array([0xff, 0x00]) },
      ]),
    );
    const binary = await readFileViaWorkspaceApi("s-server", "blob.bin");

    expect(binary).toMatchObject({ encoding: "base64", content: "/wA=", content_type: null });
    expect(binary?.truncated).toBe(true);

    serve(() =>
      stream([
        { metadata: { size: 5n, encoding: ContentEncoding.UTF8 } },
        { data: new Uint8Array([0xef, 0xbb, 0xbf, 0x68, 0x69]) },
      ]),
    );
    const withBom = await readFileViaWorkspaceApi("s-server", "bom.txt");

    expect(withBom?.content).toBe("\ufeffhi");
  });

  it("reads from the host's socket on the desktop, with one grant for many reads", async () => {
    enable("workspace_direct_access", "workspace_api_server_access");
    serve(() => {
      throw new Error("the server route must not be used");
    });
    socket.mockImplementation(async () => ({
      status: 200,
      headers: CONNECT_HEADERS,
      body: textFile(),
    }));

    expect((await readFileViaWorkspaceApi("s-direct", "notes.md"))?.content).toBe("héllo");
    expect((await readFileViaWorkspaceApi("s-direct", "notes.md"))?.content).toBe("héllo");

    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/s-direct/workspace/direct-access",
      expect.objectContaining({ method: "POST" }),
    );
    expect(socket).toHaveBeenCalledTimes(2);
    expect(socket.mock.calls[0]![0]).toMatchObject({
      hostId: "host-1",
      socketPath: "/tmp/omnigent-workspace-x/workspace.sock",
      path: `/${READ_FILE}`,
      headers: { authorization: "Bearer tok" },
    });
  });

  it("falls back to the server when the socket fails, then stops trying the socket", async () => {
    enable("workspace_direct_access", "workspace_api_server_access");
    serve(textFile);
    socket.mockRejectedValue(new Error("not a workspace socket on this machine"));

    expect((await readFileViaWorkspaceApi("s-remote", "notes.md"))?.content).toBe("héllo");
    expect((await readFileViaWorkspaceApi("s-remote", "notes.md"))?.content).toBe("héllo");

    expect(socket).toHaveBeenCalledTimes(1);
  });

  it("offers the host's SSH alias, and retries the socket only for a new one", async () => {
    enable("workspace_direct_access", "workspace_api_server_access");
    serve(textFile);
    socket.mockRejectedValueOnce(new Error("not a workspace socket on this machine"));
    socket.mockRejectedValueOnce(new Error("SSH alias not allowed or not verified for this host"));
    socket.mockImplementation(async () => fromSocket(textFile()));

    await readFileViaWorkspaceApi("s-alias", "notes.md");
    await readFileViaWorkspaceApi("s-alias", "notes.md", undefined, "my-box");
    await readFileViaWorkspaceApi("s-alias", "notes.md", undefined, "my-box");
    await readFileViaWorkspaceApi("s-alias", "notes.md", undefined, "other-box");

    expect(
      socket.mock.calls.map(([request]) => (request as { sshTarget?: string }).sshTarget),
    ).toEqual([undefined, "my-box", "other-box"]);
    expect(socket.mock.calls[0]![0]).not.toHaveProperty("sshTarget");
    expect(socket.mock.calls[1]![0]).toMatchObject({ hostId: "host-1" });
  });

  it.each(["not_found", "unavailable"])(
    "leaves a %s error to REST without giving up on the route",
    async (code) => {
      enable("workspace_api_server_access");
      serve(() => stream([], { error: { code, message: "no" } }));

      expect(await readFileViaWorkspaceApi(`s-${code}`, "missing.md")).toBeNull();
      expect(await readFileViaWorkspaceApi(`s-${code}`, "missing.md")).toBeNull();

      expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    },
  );

  it("stops waiting for a stalled grant when the read is cancelled", async () => {
    enable("workspace_direct_access", "workspace_api_server_access");
    vi.mocked(authenticatedFetch).mockReturnValue(new Promise<Response>(() => {}));
    const cancel = new AbortController();

    const read = readFileViaWorkspaceApi("s-stalled", "notes.md", cancel.signal);
    cancel.abort(new Error("save timed out"));

    await expect(read).rejects.toThrow("save timed out");
    expect(socket).not.toHaveBeenCalled();
  });

  it("renews a grant before it expires and after the host restarts", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    enable("workspace_direct_access", "workspace_api_server_access");
    serve(textFile);
    socket.mockImplementation(async () => fromSocket(textFile()));

    await readFileViaWorkspaceApi("s-renew", "notes.md");
    vi.setSystemTime(Date.now() + 280_000); // within the renewal margin
    await readFileViaWorkspaceApi("s-renew", "notes.md");
    expect(grantRequests()).toBe(2);

    // A restarted host refuses the old token: the server answers this read,
    // and the next one goes back to the socket with a new grant.
    const refused = new TextEncoder().encode('{"code":"unauthenticated"}');
    socket.mockResolvedValueOnce(fromSocket(refused, 401));
    expect((await readFileViaWorkspaceApi("s-renew", "notes.md"))?.content).toBe("héllo");
    await readFileViaWorkspaceApi("s-renew", "notes.md");

    expect(grantRequests()).toBe(3);
    expect(socket).toHaveBeenCalledTimes(4);
  });

  it("tries a failed socket again once its back-off ends", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    enable("workspace_direct_access", "workspace_api_server_access");
    serve(textFile);
    socket.mockRejectedValueOnce(new Error("socket gone"));
    socket.mockImplementation(async () => fromSocket(textFile()));

    await readFileViaWorkspaceApi("s-backoff", "notes.md");
    await readFileViaWorkspaceApi("s-backoff", "notes.md");
    expect(socket).toHaveBeenCalledTimes(1);

    vi.setSystemTime(Date.now() + 5 * 60_000 + 1);
    await readFileViaWorkspaceApi("s-backoff", "notes.md");

    expect(socket).toHaveBeenCalledTimes(2);
  });

  it("lists a directory through the server in the REST listing's shape, page by page", async () => {
    enable("workspace_api_server_access");
    const tokens: string[] = [];
    vi.mocked(authenticatedFetch).mockImplementation(async (_input, init) => {
      const request = fromBinary(ListDirectoryRequestSchema, init!.body as Uint8Array);
      tokens.push(request.pageToken);
      const page = request.pageToken
        ? { entries: [{ name: "b.ts", path: "src/b.ts", type: EntryType.FILE, size: 3n }] }
        : {
            entries: [
              {
                name: "a",
                path: "src/a",
                type: EntryType.DIRECTORY,
                modifyTime: { seconds: 1700000000n },
              },
            ],
            nextPageToken: "src/a",
          };
      return new Response(
        toBinary(ListDirectoryResponseSchema, create(ListDirectoryResponseSchema, page)),
        { headers: { "content-type": "application/proto" } },
      );
    });

    const listed = await listDirectoryViaWorkspaceApi("s-list", "src", 1000);

    expect(listed).toEqual({
      data: [
        {
          id: "src/a",
          name: "a",
          path: "src/a",
          type: "directory",
          bytes: null,
          modified_at: 1700000000,
        },
        {
          id: "src/b.ts",
          name: "b.ts",
          path: "src/b.ts",
          type: "file",
          bytes: 3,
          modified_at: null,
        },
      ],
      hasMore: false,
    });
    expect(tokens).toEqual(["", "src/a"]);
    expect(String(vi.mocked(authenticatedFetch).mock.calls[0]![0])).toBe(
      "/v1/sessions/s-list/workspace/omnigent.api.workspace.v1alpha1.WorkspaceService/ListDirectory",
    );

    // Stops at the caller's limit and reports that more remain.
    expect((await listDirectoryViaWorkspaceApi("s-list", "src", 1))?.hasMore).toBe(true);
    await expect(listDirectoryViaWorkspaceApi("s-list", "src", 0)).rejects.toThrow(RangeError);
  });

  it("asks for pages up to the caller's limit, ending on a partial page", async () => {
    enable("workspace_api_server_access");
    const pageSizes: number[] = [];
    vi.mocked(authenticatedFetch).mockImplementation(async (_input, init) => {
      const request = fromBinary(ListDirectoryRequestSchema, init!.body as Uint8Array);
      pageSizes.push(request.pageSize);
      const first = Number(request.pageToken || 0);
      const page = create(ListDirectoryResponseSchema, {
        entries: Array.from({ length: request.pageSize }, (_, i) => ({
          name: `${first + i}`,
          path: `src/${first + i}`,
        })),
        nextPageToken: String(first + request.pageSize),
      });
      return new Response(toBinary(ListDirectoryResponseSchema, page), {
        headers: { "content-type": "application/proto" },
      });
    });

    const listed = await listDirectoryViaWorkspaceApi("s-list-limit", "src", 1500);

    expect(pageSizes).toEqual([1000, 500]);
    expect(listed?.data).toHaveLength(1500);
    expect(listed?.data.at(-1)?.path).toBe("src/1499");
    expect(listed?.hasMore).toBe(true);
  });

  it.each([
    ["repeats its page token", 1, 2],
    ["sends an empty page with a token", 0, 1],
  ])("stops paging, and reports more, when the host %s", async (_case, entries, requests) => {
    enable("workspace_api_server_access");
    const page = create(ListDirectoryResponseSchema, {
      entries: Array.from({ length: entries }, () => ({ name: "a.ts", path: "src/a.ts" })),
      nextPageToken: "src/a.ts",
    });
    vi.mocked(authenticatedFetch).mockImplementation(
      async () =>
        new Response(toBinary(ListDirectoryResponseSchema, page), {
          headers: { "content-type": "application/proto" },
        }),
    );

    const listed = await listDirectoryViaWorkspaceApi("s-list-stalled", "src", 1000);

    expect(listed?.hasMore).toBe(true);
    expect(listed?.data.map((entry) => entry.path)).toEqual(entries ? ["src/a.ts"] : []);
    expect(authenticatedFetch).toHaveBeenCalledTimes(requests);
  });

  it("leaves a listing the host answers with an error to REST, and keeps the route for reads", async () => {
    enable("workspace_api_server_access");
    vi.mocked(authenticatedFetch).mockImplementation(async (input) =>
      String(input).endsWith("/ListDirectory")
        ? Response.json({ code: "not_found", message: "not a directory" }, { status: 404 })
        : new Response(textFile(), { headers: CONNECT_HEADERS }),
    );

    expect(await listDirectoryViaWorkspaceApi("s-list-error", "notes.md", 1000)).toBeNull();
    expect((await readFileViaWorkspaceApi("s-list-error", "notes.md"))?.content).toBe("héllo");
  });

  it.each([
    ["answers", ["src/b.ts"]],
    ["fails too", null],
  ])(
    "starts a listing over on the next route when one fails part way, and the server %s",
    async (serverOutcome, expected) => {
      enable("workspace_direct_access", "workspace_api_server_access");
      const firstPage = create(ListDirectoryResponseSchema, {
        entries: [{ name: "a.ts", path: "src/a.ts", type: EntryType.FILE }],
        nextPageToken: "src/a.ts",
      });
      socket.mockResolvedValueOnce({
        status: 200,
        headers: { "content-type": "application/proto" },
        body: toBinary(ListDirectoryResponseSchema, firstPage),
      });
      socket.mockRejectedValueOnce(new Error("the SSH connection closed"));
      const serverTokens: string[] = [];
      vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
        if (String(input).endsWith("/direct-access")) {
          return Response.json({
            host_id: "host-1",
            socket_path: "/tmp/omnigent-workspace-x/workspace.sock",
            token: "tok",
            expires_at: Date.now() / 1000 + 300,
          });
        }
        serverTokens.push(
          fromBinary(ListDirectoryRequestSchema, init!.body as Uint8Array).pageToken,
        );
        if (!expected) return Response.json({ code: "internal", message: "down" }, { status: 500 });
        const page = create(ListDirectoryResponseSchema, {
          entries: [{ name: "b.ts", path: "src/b.ts", type: EntryType.FILE }],
        });
        return new Response(toBinary(ListDirectoryResponseSchema, page), {
          headers: { "content-type": "application/proto" },
        });
      });

      const listed = await listDirectoryViaWorkspaceApi(`s-partial-${serverOutcome}`, "src", 1000);
      expect(listed && listed.data.map((entry) => entry.path)).toEqual(expected);
      expect(socket).toHaveBeenCalledTimes(2);
      expect(serverTokens).toEqual([""]);
    },
  );
});
