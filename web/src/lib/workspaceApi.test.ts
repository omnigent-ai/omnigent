import { create, fromBinary, toBinary, type MessageInitShape } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ContentEncoding,
  EntryType,
  GetCapabilitiesResponseSchema,
  ListDirectoryRequestSchema,
  ListDirectoryResponseSchema,
  ReadFileResponseSchema,
  SearchFilesRequestSchema,
  SearchFilesResponseSchema,
  SearchTextRequestSchema,
  SearchTextResponseSchema,
  ComparisonKind,
  FileSearchMode,
  ListWorkingCopyChangesRequestSchema,
  ListWorkingCopyChangesResponseSchema,
  ReadBaseFileResponseSchema,
  ResolveWorkingCopyBaseResponseSchema,
} from "@/gen/omnigent/api/workspace/v1alpha1/workspace_pb";
import { getCachedServerInfo, type ServerInfo } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { workspaceSocketBridge, type WorkspaceSocketResponse } from "@/lib/nativeBridge";
import {
  listDirectoryViaWorkspaceApi,
  readFileViaWorkspaceApi,
  listBranchCommitsViaWorkspaceApi,
  listWorkingCopyChangesViaWorkspaceApi,
  readBaseFileViaWorkspaceApi,
  resolveRepositoryContextViaWorkspaceApi,
  resolveWorkingCopyBaseViaWorkspaceApi,
  searchFilesViaWorkspaceApi,
  searchTextViaWorkspaceApi,
} from "./workspaceApi";

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
  return frames(
    messages.map((m) => toBinary(ReadFileResponseSchema, create(ReadFileResponseSchema, m))),
    end,
  );
}

function frames(payloads: Uint8Array[], end: object = {}): Uint8Array<ArrayBuffer> {
  const envelopes = [
    ...payloads.map((payload) => envelope(0, payload)),
    envelope(2, new TextEncoder().encode(JSON.stringify(end))),
  ];
  const body = new Uint8Array(envelopes.reduce((n, f) => n + f.length, 0));
  envelopes.reduce((offset, f) => (body.set(f, offset), offset + f.length), 0);
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

  it("reads through the server while the desktop holds a question, and tries the socket next time", async () => {
    enable("workspace_direct_access", "workspace_api_server_access");
    serve(textFile);
    const busy = new TextEncoder().encode('{"code":"unavailable"}');
    socket.mockResolvedValueOnce(fromSocket(busy, 503));
    socket.mockImplementation(async () => fromSocket(textFile()));

    expect(
      (await readFileViaWorkspaceApi("s-held", "notes.md", undefined, "my-box"))?.content,
    ).toBe("héllo");
    await readFileViaWorkspaceApi("s-held", "notes.md", undefined, "my-box");

    expect(socket).toHaveBeenCalledTimes(2);
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
                leafDirectoryPath: "src/a/b",
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
          leaf_directory_path: "src/a/b",
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

const PROTO_HEADERS = { "content-type": "application/proto" };
const capabilitiesBody = (...methods: string[]): Uint8Array<ArrayBuffer> =>
  toBinary(
    GetCapabilitiesResponseSchema,
    create(GetCapabilitiesResponseSchema, { capabilities: methods }),
  ) as Uint8Array<ArrayBuffer>;
const unimplemented = (): Response =>
  Response.json({ code: "unimplemented", message: "no such method" }, { status: 501 });
const methodOf = (input: unknown): string => String(input).split("/").at(-1)!;
const methodCalls = (method: string) =>
  vi.mocked(authenticatedFetch).mock.calls.filter(([input]) => methodOf(input) === method);

/** Answer server-route calls by method name; a method without a handler is unimplemented. */
function serveMethods(
  handlers: Record<string, (body: Uint8Array) => Response | Promise<Response>>,
) {
  vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
    const handler = handlers[methodOf(input)];
    return handler ? handler(init!.body as Uint8Array) : unimplemented();
  });
}

const foundEntries = new Uint8Array(
  toBinary(
    SearchFilesResponseSchema,
    create(SearchFilesResponseSchema, {
      // Paths relative to the request's `path`.
      results: [
        { entry: { name: "deep", path: "deep", type: EntryType.DIRECTORY } },
        {
          entry: {
            name: "main.ts",
            path: "deep/main.ts",
            type: EntryType.FILE,
            size: 4n,
            modifyTime: { seconds: 1700000000n },
          },
        },
      ],
      truncated: true,
      indexIncomplete: true,
    }),
  ),
);
const SEARCH_OPTIONS = { path: "src", include: "*.{ts,tsx}, lib/**", exclude: "", limit: 500 };
const SEARCH_FILES = ["SearchFiles", "search_files.substring"];

describe("searchFilesViaWorkspaceApi", () => {
  it("searches through the server in the REST search's shape", async () => {
    enable("workspace_api_server_access");
    const requests: unknown[] = [];
    serveMethods({
      GetCapabilities: () =>
        new Response(capabilitiesBody("ReadFile", ...SEARCH_FILES), { headers: PROTO_HEADERS }),
      SearchFiles: (body) => {
        requests.push(fromBinary(SearchFilesRequestSchema, body));
        return new Response(foundEntries, { headers: PROTO_HEADERS });
      },
    });

    const found = await searchFilesViaWorkspaceApi("s-find", "MAIN", SEARCH_OPTIONS);

    expect(requests).toEqual([
      expect.objectContaining({
        query: "MAIN",
        mode: FileSearchMode.SUBSTRING,
        maximumResults: 500,
        path: "src",
        include: ["*.{ts,tsx}", "lib/**"],
        exclude: [],
        includeGitIgnored: true,
      }),
    ]);
    // Paths are relative to the searched directory, as REST reports them.
    expect(found).toEqual({
      data: [
        {
          id: "deep",
          name: "deep",
          path: "deep",
          type: "directory",
          bytes: null,
          modified_at: null,
        },
        {
          id: "deep/main.ts",
          name: "main.ts",
          path: "deep/main.ts",
          type: "file",
          bytes: 4,
          modified_at: 1700000000,
        },
      ],
      hasMore: true,
      truncated: true,
    });
    // One capability check serves later searches.
    await searchFilesViaWorkspaceApi("s-find", "MAIN", SEARCH_OPTIONS);
    expect(methodCalls("GetCapabilities")).toHaveLength(1);
  });

  it.each([
    ["the method", ["ListDirectory", "ReadFile"]],
    ["substring search", ["ListDirectory", "ReadFile", "SearchFiles", "search_files.fuzzy"]],
  ])(
    "leaves the search to REST when the host doesn't list %s, keeping the route for reads",
    async (_case, listed) => {
      enable("workspace_api_server_access");
      serveMethods({
        GetCapabilities: () =>
          new Response(capabilitiesBody(...listed), { headers: PROTO_HEADERS }),
        ReadFile: () => new Response(textFile(), { headers: CONNECT_HEADERS }),
      });

      expect(await searchFilesViaWorkspaceApi("s-old-host", "main", SEARCH_OPTIONS)).toBeNull();
      expect((await readFileViaWorkspaceApi("s-old-host", "notes.md"))?.content).toBe("héllo");

      expect(methodCalls("SearchFiles")).toHaveLength(0);
    },
  );

  it("stops asking a socket for a method it refuses, keeping its grant and its reads", async () => {
    enable("workspace_direct_access");
    serve(() => {
      throw new Error("the server route is off");
    });
    socket.mockImplementation(async (request) => {
      const method = methodOf((request as { path: string }).path);
      if (method === "GetCapabilities") {
        return { status: 200, headers: PROTO_HEADERS, body: capabilitiesBody(...SEARCH_FILES) };
      }
      if (method === "ReadFile") return fromSocket(textFile());
      const refused = new TextEncoder().encode('{"code":"unimplemented"}');
      return fromSocket(refused, 501);
    });

    expect(await searchFilesViaWorkspaceApi("s-refuses", "main", SEARCH_OPTIONS)).toBeNull();
    expect(await searchFilesViaWorkspaceApi("s-refuses", "main", SEARCH_OPTIONS)).toBeNull();
    expect((await readFileViaWorkspaceApi("s-refuses", "notes.md"))?.content).toBe("héllo");

    const methods = socket.mock.calls.map(([request]) =>
      methodOf((request as { path: string }).path),
    );
    expect(methods).toEqual(["GetCapabilities", "SearchFiles", "ReadFile"]);
    expect(grantRequests()).toBe(1);
  });

  it.each(["not_found", "invalid_argument"])(
    "keeps a socket's grant when it answers %s",
    async (code) => {
      enable("workspace_direct_access");
      serve(() => {
        throw new Error("the server route is off");
      });
      // A stream's error arrives in its end message.
      socket.mockImplementation(async () =>
        fromSocket(frames([], { error: { code, message: code } })),
      );

      expect(await readFileViaWorkspaceApi(`s-answers-${code}`, "missing.md")).toBeNull();
      expect(await readFileViaWorkspaceApi(`s-answers-${code}`, "missing.md")).toBeNull();

      // An answer: the same grant serves the next read on the same route.
      expect(socket).toHaveBeenCalledTimes(2);
      expect(grantRequests()).toBe(1);
    },
  );

  it("tries a refused method again once its back-off ends", async () => {
    enable("workspace_direct_access");
    serve(() => {
      throw new Error("the server route is off");
    });
    let refusesSearch = true;
    socket.mockImplementation(async (request) => {
      const method = methodOf((request as { path: string }).path);
      if (method === "GetCapabilities") {
        return { status: 200, headers: PROTO_HEADERS, body: capabilitiesBody(...SEARCH_FILES) };
      }
      if (method === "ReadFile") return fromSocket(textFile());
      if (refusesSearch)
        return fromSocket(new TextEncoder().encode('{"code":"unimplemented"}'), 501);
      return { status: 200, headers: PROTO_HEADERS, body: foundEntries };
    });
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      expect(await searchFilesViaWorkspaceApi("s-retry", "main", SEARCH_OPTIONS)).toBeNull();

      vi.setSystemTime(Date.now() + 5 * 60_000 + 1_000);
      refusesSearch = false;
      expect(await searchFilesViaWorkspaceApi("s-retry", "main", SEARCH_OPTIONS)).not.toBeNull();
      expect((await readFileViaWorkspaceApi("s-retry", "notes.md"))?.content).toBe("héllo");
    } finally {
      vi.useRealTimers();
    }
  });

  it("reads a restarted direct host's capabilities after it refuses the token", async () => {
    enable("workspace_direct_access");
    serve(() => {
      throw new Error("the server route is off");
    });
    let restarted = false;
    socket.mockImplementation(async (request) => {
      const method = methodOf((request as { path: string }).path);
      if (method === "GetCapabilities") {
        // A restarted host serves only reads.
        const served = restarted ? ["ReadFile"] : SEARCH_FILES;
        return { status: 200, headers: PROTO_HEADERS, body: capabilitiesBody(...served) };
      }
      if (method === "ReadFile") return fromSocket(textFile());
      if (restarted) return fromSocket(new TextEncoder().encode('{"code":"unauthenticated"}'), 401);
      return { status: 200, headers: PROTO_HEADERS, body: foundEntries };
    });

    expect(await searchFilesViaWorkspaceApi("s-restart", "main", SEARCH_OPTIONS)).not.toBeNull();
    restarted = true;
    // The refused token ends this search, and the next one asks what the restarted host serves.
    expect(await searchFilesViaWorkspaceApi("s-restart", "main", SEARCH_OPTIONS)).toBeNull();
    expect(await searchFilesViaWorkspaceApi("s-restart", "main", SEARCH_OPTIONS)).toBeNull();

    const methods = socket.mock.calls.map(([request]) =>
      methodOf((request as { path: string }).path),
    );
    expect(methods.filter((method) => method === "GetCapabilities")).toHaveLength(2);
    expect(methods.filter((method) => method === "SearchFiles")).toHaveLength(2);
    expect(grantRequests()).toBe(2);
  });

  it("leaves the search to REST while the host's file list is still being built", async () => {
    enable("workspace_api_server_access");
    const refreshing = create(SearchFilesResponseSchema, { refreshing: true });
    serveMethods({
      GetCapabilities: () =>
        new Response(capabilitiesBody("ReadFile", ...SEARCH_FILES), { headers: PROTO_HEADERS }),
      SearchFiles: () =>
        new Response(toBinary(SearchFilesResponseSchema, refreshing), { headers: PROTO_HEADERS }),
      ReadFile: () => new Response(textFile(), { headers: CONNECT_HEADERS }),
    });

    expect(await searchFilesViaWorkspaceApi("s-refreshing", "main", SEARCH_OPTIONS)).toBeNull();
    // An unsettled list isn't a failure: the route keeps serving.
    expect((await readFileViaWorkspaceApi("s-refreshing", "notes.md"))?.content).toBe("héllo");
    expect(await searchFilesViaWorkspaceApi("s-refreshing", "main", SEARCH_OPTIONS)).toBeNull();
    expect(methodCalls("SearchFiles")).toHaveLength(2);
  });

  it("lets one caller cancel while another uses the shared capability check", async () => {
    enable("workspace_api_server_access");
    let answerCapabilities!: () => void;
    const capabilitiesReady = new Promise<void>((resolve) => {
      answerCapabilities = resolve;
    });
    serveMethods({
      GetCapabilities: async () => {
        await capabilitiesReady;
        return new Response(capabilitiesBody("ReadFile", ...SEARCH_FILES), {
          headers: PROTO_HEADERS,
        });
      },
      SearchFiles: () => new Response(foundEntries, { headers: PROTO_HEADERS }),
      ReadFile: () => new Response(textFile(), { headers: CONNECT_HEADERS }),
    });
    const cancel = new AbortController();

    const cancelled = searchFilesViaWorkspaceApi("s-shared", "ma", SEARCH_OPTIONS, cancel.signal);
    const kept = searchFilesViaWorkspaceApi("s-shared", "main", SEARCH_OPTIONS);
    cancel.abort(new Error("query replaced"));
    answerCapabilities();

    await expect(cancelled).rejects.toThrow("query replaced");
    expect((await kept)?.data).toHaveLength(2);
    expect(methodCalls("GetCapabilities")).toHaveLength(1);
    expect(methodCalls("SearchFiles")).toHaveLength(1);
    // The route wasn't backed off.
    expect(
      (await searchFilesViaWorkspaceApi("s-shared", "main", SEARCH_OPTIONS))?.data,
    ).toHaveLength(2);
  });

  it("backs the route off when the capability check fails", async () => {
    enable("workspace_api_server_access");
    serveMethods({
      GetCapabilities: () => Response.json({ code: "internal", message: "down" }, { status: 500 }),
    });

    expect(await searchFilesViaWorkspaceApi("s-check-fails", "main", SEARCH_OPTIONS)).toBeNull();
    expect(await readFileViaWorkspaceApi("s-check-fails", "notes.md")).toBeNull();

    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it("searches through REST on a host without the capability query, keeping its reads", async () => {
    enable("workspace_api_server_access");
    // An older host has no GetCapabilities, so it answers unimplemented.
    serveMethods({
      ReadFile: () => new Response(textFile(), { headers: CONNECT_HEADERS }),
    });

    expect(
      await searchFilesViaWorkspaceApi("s-no-capabilities", "main", SEARCH_OPTIONS),
    ).toBeNull();
    expect(
      await searchFilesViaWorkspaceApi("s-no-capabilities", "main", SEARCH_OPTIONS),
    ).toBeNull();
    expect((await readFileViaWorkspaceApi("s-no-capabilities", "notes.md"))?.content).toBe("héllo");

    // Every method on this route skips it, so the capability query isn't repeated.
    enable("workspace_content_search");
    expect(await searchTextViaWorkspaceApi("s-no-capabilities", "needle", TEXT_OPTIONS)).toBeNull();
    expect(methodCalls("GetCapabilities")).toHaveLength(1);
    expect(methodCalls("SearchFiles")).toHaveLength(0);
  });

  it("falls back to REST when the capability check stalls", async () => {
    enable("workspace_api_server_access");
    vi.useFakeTimers();
    try {
      // Like fetch, a stalled call rejects once its signal aborts.
      vi.mocked(authenticatedFetch).mockImplementation(
        (_input, init) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
          }),
      );

      const search = searchFilesViaWorkspaceApi("s-stalled", "main", SEARCH_OPTIONS);
      await vi.advanceTimersByTimeAsync(10_000);

      expect(await search).toBeNull();
      expect(methodCalls("SearchFiles")).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reads a host's capabilities again once the cached answer expires", async () => {
    enable("workspace_api_server_access");
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      serveMethods({
        GetCapabilities: () =>
          new Response(capabilitiesBody("ReadFile", ...SEARCH_FILES), { headers: PROTO_HEADERS }),
        SearchFiles: () => new Response(foundEntries, { headers: PROTO_HEADERS }),
      });

      expect(await searchFilesViaWorkspaceApi("s-expiry", "main", SEARCH_OPTIONS)).not.toBeNull();
      vi.setSystemTime(Date.now() + 5 * 60_000 + 1_000);
      expect(await searchFilesViaWorkspaceApi("s-expiry", "main", SEARCH_OPTIONS)).not.toBeNull();

      expect(methodCalls("GetCapabilities")).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });
});

const textMatches = (...messages: MessageInitShape<typeof SearchTextResponseSchema>[]) =>
  frames(
    messages.map((m) => toBinary(SearchTextResponseSchema, create(SearchTextResponseSchema, m))),
  );
const match = (line: number) => ({
  path: "src/a.ts",
  line: BigInt(line),
  column: 3,
  text: "x needle",
  matchStart: 2,
  matchEnd: 8,
});
const TEXT_OPTIONS = { caseSensitive: true, regex: false, include: "src/**", exclude: "" };

describe("searchTextViaWorkspaceApi", () => {
  it("collects the streamed matches in the REST content search's shape", async () => {
    enable("workspace_api_server_access", "workspace_content_search");
    const requests: unknown[] = [];
    serveMethods({
      GetCapabilities: () =>
        new Response(capabilitiesBody("SearchText"), { headers: PROTO_HEADERS }),
      SearchText: (body) => {
        requests.push(fromBinary(SearchTextRequestSchema, envelopePayload(body)));
        return new Response(
          textMatches(
            { matches: [match(1), match(2)] },
            { matches: [match(3)], done: true, truncated: true, warning: "Search stopped." },
          ),
          { headers: CONNECT_HEADERS },
        );
      },
    });

    const found = await searchTextViaWorkspaceApi("s-text", "needle", TEXT_OPTIONS);

    expect(requests).toEqual([
      expect.objectContaining({ query: "needle", caseSensitive: true, include: "src/**" }),
    ]);
    expect(found).toEqual({
      matches: [1, 2, 3].map((line) => ({
        path: "src/a.ts",
        line,
        column: 3,
        text: "x needle",
        match_start: 2,
        match_end: 8,
      })),
      truncated: true,
      warning: "Search stopped.",
    });
  });

  it("leaves a stream that ends without its last message to REST, then stops asking", async () => {
    enable("workspace_api_server_access", "workspace_content_search");
    serveMethods({
      GetCapabilities: () =>
        new Response(capabilitiesBody("SearchText"), { headers: PROTO_HEADERS }),
      SearchText: () =>
        new Response(textMatches({ matches: [match(1)] }), { headers: CONNECT_HEADERS }),
      ReadFile: () => new Response(textFile(), { headers: CONNECT_HEADERS }),
    });

    expect(await searchTextViaWorkspaceApi("s-text-cut", "needle", TEXT_OPTIONS)).toBeNull();
    expect(await searchTextViaWorkspaceApi("s-text-cut", "needle", TEXT_OPTIONS)).toBeNull();

    // Only the method backs off: the route still reads files.
    expect(methodCalls("SearchText")).toHaveLength(1);
    expect((await readFileViaWorkspaceApi("s-text-cut", "notes.md"))?.content).toBe("héllo");
  });

  it("leaves the search to REST while content search is off", async () => {
    enable("workspace_direct_access", "workspace_api_server_access");

    expect(await searchTextViaWorkspaceApi("s-text-off", "needle", TEXT_OPTIONS)).toBeNull();
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(socket).not.toHaveBeenCalled();
  });

  it("leaves an invalid search to REST, which reports it", async () => {
    enable("workspace_api_server_access", "workspace_content_search");
    serveMethods({
      GetCapabilities: () =>
        new Response(capabilitiesBody("SearchText"), { headers: PROTO_HEADERS }),
      SearchText: () =>
        new Response(frames([], { error: { code: "invalid_argument", message: "bad regex" } }), {
          headers: CONNECT_HEADERS,
        }),
    });

    expect(await searchTextViaWorkspaceApi("s-text-bad", "[", TEXT_OPTIONS)).toBeNull();
    expect(await searchTextViaWorkspaceApi("s-text-bad", "[", TEXT_OPTIONS)).toBeNull();
    // An answer, not a failure: the second search asks the API again.
    expect(methodCalls("SearchText")).toHaveLength(2);
  });
});

describe("Git comparisons through the workspace API", () => {
  it("lists a comparison's changes and reads a base file in the REST payload's shape", async () => {
    enable("workspace_api_server_access");
    const comparisons: unknown[] = [];
    serveMethods({
      GetCapabilities: () =>
        new Response(capabilitiesBody("ListWorkingCopyChanges", "ReadBaseFile"), {
          headers: PROTO_HEADERS,
        }),
      ListWorkingCopyChanges: (body) => {
        comparisons.push(fromBinary(ListWorkingCopyChangesRequestSchema, body).comparison);
        const changes = create(ListWorkingCopyChangesResponseSchema, {
          base: { kind: ComparisonKind.BRANCH_POINT, base: "b".repeat(40) },
          files: [{ path: "src/a.ts", baseObject: "c".repeat(40) }],
        });
        return new Response(toBinary(ListWorkingCopyChangesResponseSchema, changes), {
          headers: PROTO_HEADERS,
        });
      },
      ReadBaseFile: () =>
        new Response(
          frames([
            toBinary(
              ReadBaseFileResponseSchema,
              create(ReadBaseFileResponseSchema, {
                metadata: { size: 2n, encoding: ContentEncoding.UTF8 },
                data: new TextEncoder().encode("hi"),
              }),
            ),
          ]),
          { headers: CONNECT_HEADERS },
        ),
    });

    const changes = await listWorkingCopyChangesViaWorkspaceApi("s-git", {
      kind: ComparisonKind.BRANCH_POINT,
    });
    const base = await readBaseFileViaWorkspaceApi("s-git", {
      commit: changes!.base!.base,
      path: changes!.files[0]!.path,
      object: changes!.files[0]!.baseObject,
    });

    expect(comparisons).toEqual([expect.objectContaining({ kind: ComparisonKind.BRANCH_POINT })]);
    expect(base).toMatchObject({ path: "src/a.ts", encoding: "utf-8", content: "hi", bytes: 2 });
  });

  it("answers null when the host doesn't serve the method", async () => {
    enable("workspace_api_server_access");
    serveMethods({
      GetCapabilities: () =>
        new Response(capabilitiesBody("ListDirectory", "ReadFile"), { headers: PROTO_HEADERS }),
    });

    expect(await resolveRepositoryContextViaWorkspaceApi("s-git-old")).toBeNull();
    expect(methodCalls("ResolveRepositoryContext")).toHaveLength(0);
  });

  it("resolves a comparison's base, and stops asking a host that answers without one", async () => {
    enable("workspace_api_server_access");
    let sendsBase = true;
    serveMethods({
      GetCapabilities: () =>
        new Response(capabilitiesBody("ResolveWorkingCopyBase"), { headers: PROTO_HEADERS }),
      ResolveWorkingCopyBase: () => {
        const response = create(ResolveWorkingCopyBaseResponseSchema, {
          base: sendsBase ? { kind: ComparisonKind.LAST_COMMIT, base: "a".repeat(40) } : undefined,
        });
        return new Response(toBinary(ResolveWorkingCopyBaseResponseSchema, response), {
          headers: PROTO_HEADERS,
        });
      },
      ReadFile: () => new Response(textFile(), { headers: CONNECT_HEADERS }),
    });
    const lastCommit = { kind: ComparisonKind.LAST_COMMIT };

    expect(await resolveWorkingCopyBaseViaWorkspaceApi("s-git-base", lastCommit)).toMatchObject({
      base: "a".repeat(40),
    });
    sendsBase = false;
    expect(await resolveWorkingCopyBaseViaWorkspaceApi("s-git-base", lastCommit)).toBeNull();
    expect(await resolveWorkingCopyBaseViaWorkspaceApi("s-git-base", lastCommit)).toBeNull();

    // An unusable answer backs off that method alone.
    expect(methodCalls("ResolveWorkingCopyBase")).toHaveLength(2);
    expect((await readFileViaWorkspaceApi("s-git-base", "notes.md"))?.content).toBe("héllo");
  });

  it("hands the host's refusal of a Git request to the caller, keeping the route", async () => {
    enable("workspace_api_server_access");
    serveMethods({
      GetCapabilities: () =>
        new Response(capabilitiesBody("ListBranchCommits", "ReadBaseFile"), {
          headers: PROTO_HEADERS,
        }),
      ListBranchCommits: () =>
        Response.json({ code: "not_found", message: "No revision 'gone'" }, { status: 404 }),
      // The base moved since the changes were listed.
      ReadBaseFile: () =>
        new Response(
          frames([], { error: { code: "failed_precondition", message: "The blob changed" } }),
          { headers: CONNECT_HEADERS },
        ),
      ReadFile: () => new Response(textFile(), { headers: CONNECT_HEADERS }),
    });
    const file = { commit: "b".repeat(40), path: "src/a.ts", object: "c".repeat(40) };

    const commits = listBranchCommitsViaWorkspaceApi("s-git-refused", "gone");
    await expect(commits).rejects.toThrow("No revision 'gone'");
    await expect(commits).rejects.toMatchObject({ code: Code.NotFound });
    const base = readBaseFileViaWorkspaceApi("s-git-refused", file);
    await expect(base).rejects.toBeInstanceOf(ConnectError);
    await expect(base).rejects.toMatchObject({ code: Code.FailedPrecondition });

    // Answers, not failures: the route keeps serving reads and asks again.
    expect((await readFileViaWorkspaceApi("s-git-refused", "notes.md"))?.content).toBe("héllo");
    await expect(readBaseFileViaWorkspaceApi("s-git-refused", file)).rejects.toThrow();
    expect(methodCalls("ReadBaseFile")).toHaveLength(2);
  });
});

/** The message inside a one-message Connect streaming request body. */
function envelopePayload(body: Uint8Array): Uint8Array {
  return body.subarray(5, 5 + new DataView(body.buffer, body.byteOffset).getUint32(1));
}
