import { create, toBinary, type MessageInitShape } from "@bufbuild/protobuf";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ContentEncoding,
  ReadFileResponseSchema,
} from "@/gen/omnigent/api/workspace/v1alpha1/workspace_pb";
import { getCachedServerInfo, type ServerInfo } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { workspaceSocketBridge, type WorkspaceSocketResponse } from "@/lib/nativeBridge";
import { readFileViaWorkspaceApi } from "./workspaceApi";

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
        socket_path: "/tmp/omnigent-workspace-x/workspace.sock",
        token: "tok",
        expires_at: Date.now() / 1000 + 300,
      });
    }
    return new Response(readFile(), { headers: CONNECT_HEADERS });
  });
}

const socket = vi.fn<(request: unknown) => Promise<WorkspaceSocketResponse>>();

beforeEach(() => {
  vi.mocked(workspaceSocketBridge).mockReturnValue(socket);
});

afterEach(() => {
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
      { method: "POST" },
    );
    expect(socket).toHaveBeenCalledTimes(2);
    expect(socket.mock.calls[0]![0]).toMatchObject({
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

  it("leaves an error answer to REST without giving up on the route", async () => {
    enable("workspace_api_server_access");
    serve(() => stream([], { error: { code: "not_found", message: "missing.md" } }));

    expect(await readFileViaWorkspaceApi("s-missing", "missing.md")).toBeNull();
    expect(await readFileViaWorkspaceApi("s-missing", "missing.md")).toBeNull();

    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });
});
