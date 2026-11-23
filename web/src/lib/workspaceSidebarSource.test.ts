import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authenticatedFetch = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch }));

import type { FileContentResponse } from "@/hooks/useFileContent";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";

import {
  ComparisonKind,
  DEFAULT_WORKSPACE_SIDEBAR_API,
  setWorkspaceSidebarApiForTests,
  type WorkspaceSidebarApi,
} from "./workspaceSidebarApi";
import {
  DEFAULT_WORKSPACE_SIDEBAR_SOURCE,
  fileContentText,
  type HostOnlyMethod,
  NOT_AVAILABLE_ON_HOST,
  setWorkspaceSidebarSourceForTests,
  WorkspaceMethodUnavailableError,
  workspaceSidebarSource,
} from "./workspaceSidebarSource";

const SESSION = "conv_source";
const source = DEFAULT_WORKSPACE_SIDEBAR_SOURCE;
const RESOURCES = `/v1/sessions/${SESSION}/resources/environments/default`;
const FILE_OPTIONS = { path: "", include: "", exclude: "", limit: 20 };
const TEXT_OPTIONS = { caseSensitive: true, regex: false, include: "*.ts", exclude: "" };

const unserved = () => Promise.resolve(null);
/** An API where nothing is served, so every read falls back or is unavailable. */
const NOTHING_SERVED: WorkspaceSidebarApi = {
  listDirectory: unserved,
  readFile: unserved,
  searchFiles: unserved,
  searchText: unserved,
  resolveWorkingCopyBase: unserved,
  listWorkingCopyChanges: unserved,
  readBaseFile: unserved,
  readLinkText: unserved,
  listRepositoryRefs: unserved,
  listBranchCommits: unserved,
  resolveRepositoryContext: unserved,
};

function serving(methods: Partial<WorkspaceSidebarApi>) {
  setWorkspaceSidebarApiForTests({ ...NOTHING_SERVED, ...methods });
}

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const entry = (path: string) => ({
  id: path,
  name: path.split("/").at(-1) ?? path,
  path,
  type: "file" as const,
  bytes: 3,
  modified_at: 1,
});

const file = (content: string, extra: Partial<FileContentResponse> = {}): FileContentResponse => ({
  object: "session.environment.filesystem.file_content",
  path: "a.ts",
  content_type: "text/plain",
  encoding: "utf-8",
  content,
  bytes: content.length,
  ...extra,
});

/** The path and query of the REST request last sent. */
function lastRequest(): URL {
  return new URL(String(authenticatedFetch.mock.lastCall?.[0]), "https://app.example.com");
}

beforeEach(() => serving({}));

afterEach(() => {
  setWorkspaceSidebarApiForTests(null);
  setWorkspaceSidebarSourceForTests(null);
  authenticatedFetch.mockReset();
});

describe("reads the workspace API serves", () => {
  it("takes each served answer as it is, without REST", async () => {
    const listed = { data: [entry("a.ts")], hasMore: true };
    const found = { data: [entry("b.ts")], hasMore: true, truncated: true };
    const matches = { matches: [], truncated: true, warning: "Stopped at 1,000 files." };
    serving({
      listDirectory: async () => listed,
      readFile: async () => file("text"),
      searchFiles: async () => found,
      searchText: async () => matches,
    });
    await expect(source.listDirectory(SESSION, "", 100)).resolves.toBe(listed);
    await expect(source.readFile(SESSION, "a.ts")).resolves.toEqual(file("text"));
    await expect(source.searchFiles(SESSION, "b", FILE_OPTIONS)).resolves.toBe(found);
    await expect(source.searchText(SESSION, "x", TEXT_OPTIONS)).resolves.toBe(matches);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("passes each Git answer through", async () => {
    const base = {
      kind: ComparisonKind.LAST_COMMIT,
      head: "h",
      base: "h",
      reference: "",
      referenceCommit: "",
      mergeBase: "",
      baseDisplay: "HEAD",
      headDisplay: "",
      headIsWorkingCopy: true,
    };
    const refs = { refs: ["main"], truncated: false, defaultBranchRef: "origin/main" };
    serving({
      resolveWorkingCopyBase: async () => base,
      listRepositoryRefs: async () => refs,
      readLinkText: async () => new Uint8Array([97]),
    });
    await expect(
      source.resolveWorkingCopyBase(SESSION, { kind: ComparisonKind.LAST_COMMIT }),
    ).resolves.toBe(base);
    await expect(source.listRepositoryRefs(SESSION)).resolves.toBe(refs);
    await expect(source.readLinkText(SESSION, "link")).resolves.toEqual(new Uint8Array([97]));
  });

  it("lets a host's refusal through unchanged", async () => {
    const refusal = Object.assign(new Error("[failed_precondition] base moved"), {
      name: "ConnectError",
      code: 9,
    });
    serving({
      readBaseFile: () => Promise.reject(refusal),
      listDirectory: () => Promise.reject(refusal),
    });
    await expect(
      source.readBaseFile(SESSION, { commit: "c", path: "a.ts", object: "o" }),
    ).rejects.toBe(refusal);
    await expect(source.listDirectory(SESSION, "", 10)).rejects.toBe(refusal);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("REST in place of the workspace API", () => {
  it("lists a directory, keeping whether there is more", async () => {
    authenticatedFetch.mockResolvedValue(
      json({
        data: [{ name: "a.ts", path: "src/a.ts", type: "file", bytes: 3, modified_at: 1 }],
        has_more: true,
      }),
    );
    await expect(source.listDirectory(SESSION, "src", 100)).resolves.toEqual({
      data: [{ ...entry("src/a.ts") }],
      hasMore: true,
    });
    expect(lastRequest().pathname).toBe(`${RESOURCES}/filesystem/src`);
    expect(Object.fromEntries(lastRequest().searchParams)).toEqual({ limit: "100", order: "asc" });
  });

  it("lists and reads outside the workspace through REST alone", async () => {
    const listDirectory = vi.fn(async () => ({ data: [], hasMore: false }));
    const readFile = vi.fn(async () => file("never"));
    serving({ listDirectory, readFile });
    authenticatedFetch.mockResolvedValue(json({ data: [] }));
    await source.listDirectory(SESSION, "/etc", 10);
    expect(lastRequest().pathname).toBe(`${RESOURCES}/filesystem/etc`);
    expect(lastRequest().searchParams.get("base")).toBe("host");
    authenticatedFetch.mockResolvedValue(json(file("host text")));
    await expect(source.readFile(SESSION, "/etc/hosts")).resolves.toMatchObject({
      content: "host text",
    });
    expect(lastRequest().searchParams.get("base")).toBe("host");
    expect(listDirectory).not.toHaveBeenCalled();
    expect(readFile).not.toHaveBeenCalled();
  });

  it("reads a file once through REST, without asking the API again", async () => {
    const readFile = vi.fn(unserved);
    serving({ readFile });
    authenticatedFetch.mockResolvedValue(json(file("from rest")));
    await expect(source.readFile(SESSION, "a.ts")).resolves.toMatchObject({ content: "from rest" });
    expect(readFile).toHaveBeenCalledTimes(1);
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(lastRequest().pathname).toBe(`${RESOURCES}/filesystem/a.ts`);
  });

  it("searches file names, keeping whether more matched and whether the search stopped early", async () => {
    authenticatedFetch.mockResolvedValue(
      json({
        data: [{ name: "a.ts", path: "a.ts", type: "file", bytes: 3, modified_at: 1 }],
        has_more: true,
        truncated: true,
      }),
    );
    await expect(
      source.searchFiles(SESSION, "a", { ...FILE_OPTIONS, path: "src", include: "*.ts" }),
    ).resolves.toEqual({ data: [entry("a.ts")], hasMore: true, truncated: true });
    expect(lastRequest().pathname).toBe(`${RESOURCES}/search/src`);
    expect(Object.fromEntries(lastRequest().searchParams)).toEqual({
      q: "a",
      limit: "20",
      include: "*.ts",
    });
  });

  it("searches file contents, keeping a truncated result and its warning", async () => {
    const match = { path: "a.ts", line: 1, column: 1, text: "x", match_start: 0, match_end: 1 };
    authenticatedFetch.mockResolvedValue(
      json({ matches: [match], truncated: true, warning: "Stopped at 1,000 files." }),
    );
    await expect(source.searchText(SESSION, "x", TEXT_OPTIONS)).resolves.toEqual({
      matches: [match],
      truncated: true,
      warning: "Stopped at 1,000 files.",
    });
    expect(lastRequest().pathname).toBe(`${RESOURCES}/content-search`);
    expect(Object.fromEntries(lastRequest().searchParams)).toEqual({
      q: "x",
      case_sensitive: "true",
      regex: "false",
      include: "*.ts",
      exclude: "",
    });
    authenticatedFetch.mockResolvedValue(json({ matches: [] }));
    await expect(source.searchText(SESSION, "x", TEXT_OPTIONS)).resolves.toEqual({
      matches: [],
      truncated: false,
      warning: null,
    });
  });

  it("keeps the failures the panels tell apart", async () => {
    authenticatedFetch.mockResolvedValue(
      json({ error: { code: "runner_unavailable" } }, { status: 503 }),
    );
    await expect(source.listDirectory(SESSION, "", 10)).rejects.toBeInstanceOf(RunnerOfflineError);
    authenticatedFetch.mockResolvedValue(
      new Response("bad gateway", { status: 502, statusText: "Bad Gateway" }),
    );
    await expect(source.searchFiles(SESSION, "a", FILE_OPTIONS)).rejects.toThrow("502 Bad Gateway");
    authenticatedFetch.mockResolvedValue(
      json({ error: { message: "Invalid regex" } }, { status: 400 }),
    );
    await expect(source.searchText(SESSION, "(", TEXT_OPTIONS)).rejects.toThrow("Invalid regex");
    authenticatedFetch.mockResolvedValue(new Response("not json", { status: 500 }));
    await expect(source.searchText(SESSION, "x", TEXT_OPTIONS)).rejects.toThrow(
      "Content search is unavailable. Check that the runner is online and up to date.",
    );
  });
});

describe("Git reads no route serves", () => {
  const calls: [HostOnlyMethod, () => Promise<unknown>][] = [
    [
      "resolveWorkingCopyBase",
      () => source.resolveWorkingCopyBase(SESSION, { kind: ComparisonKind.BRANCH_POINT }),
    ],
    [
      "listWorkingCopyChanges",
      () =>
        source.listWorkingCopyChanges(SESSION, {
          kind: ComparisonKind.REVISIONS,
          baseRevision: "v1",
          headRevision: "",
        }),
    ],
    ["readBaseFile", () => source.readBaseFile(SESSION, { commit: "c", path: "a", object: "o" })],
    ["readLinkText", () => source.readLinkText(SESSION, "link")],
    ["listRepositoryRefs", () => source.listRepositoryRefs(SESSION)],
    ["listBranchCommits", () => source.listBranchCommits(SESSION, "")],
    ["resolveRepositoryContext", () => source.resolveRepositoryContext(SESSION)],
  ];

  it.each(calls)("says %s isn't available on this host, without REST", async (method, call) => {
    const refusal = await call().catch((error: unknown) => error);
    expect(refusal).toBeInstanceOf(WorkspaceMethodUnavailableError);
    expect(refusal).toMatchObject({ method, reason: "not_served", message: NOT_AVAILABLE_ON_HOST });
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("are unavailable by default, as today's API serves none of them", async () => {
    setWorkspaceSidebarApiForTests(DEFAULT_WORKSPACE_SIDEBAR_API);
    await expect(source.listBranchCommits(SESSION, "")).rejects.toBeInstanceOf(
      WorkspaceMethodUnavailableError,
    );
    await expect(source.servesGitComparisons(SESSION)).resolves.toBe(false);
  });
});

describe("servesGitComparisons", () => {
  it("asks for the last commit: served when the host answers, even with a refusal", async () => {
    const resolveWorkingCopyBase = vi.fn(unserved);
    serving({ resolveWorkingCopyBase });
    await expect(source.servesGitComparisons(SESSION)).resolves.toBe(false);
    expect(resolveWorkingCopyBase).toHaveBeenCalledWith(
      SESSION,
      { kind: ComparisonKind.LAST_COMMIT },
      undefined,
    );
    serving({
      resolveWorkingCopyBase: () =>
        Promise.reject(Object.assign(new Error("not a repository"), { name: "ConnectError" })),
    });
    await expect(source.servesGitComparisons(SESSION)).resolves.toBe(true);
  });

  it("passes on a failure to ask", async () => {
    const offline = new TypeError("Failed to fetch");
    serving({ resolveWorkingCopyBase: () => Promise.reject(offline) });
    await expect(source.servesGitComparisons(SESSION)).rejects.toBe(offline);
  });
});

describe("aborted reads", () => {
  it("end with the caller's reason, whichever layer noticed", async () => {
    const controller = new AbortController();
    const reason = new DOMException("The panel closed.", "AbortError");
    serving({
      // One read answers after the abort; the other fails because of it.
      listDirectory: async () => {
        controller.abort(reason);
        return { data: [], hasMore: false };
      },
      listBranchCommits: async () => {
        controller.abort(reason);
        throw new Error("request cancelled");
      },
      resolveWorkingCopyBase: async () => {
        controller.abort(reason);
        return null;
      },
    });
    await expect(source.listDirectory(SESSION, "", 10, controller.signal)).rejects.toBe(reason);
    await expect(source.listBranchCommits(SESSION, "", controller.signal)).rejects.toBe(reason);
    await expect(source.servesGitComparisons(SESSION, controller.signal)).rejects.toBe(reason);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("don't fall back to REST once aborted", async () => {
    const controller = new AbortController();
    serving({
      searchFiles: async () => {
        controller.abort();
        return null;
      },
    });
    await expect(source.searchFiles(SESSION, "a", FILE_OPTIONS, controller.signal)).rejects.toBe(
      controller.signal.reason,
    );
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("setWorkspaceSidebarSourceForTests", () => {
  it("swaps in a fake, and null restores the default", () => {
    expect(workspaceSidebarSource()).toBe(DEFAULT_WORKSPACE_SIDEBAR_SOURCE);
    const fake = { ...DEFAULT_WORKSPACE_SIDEBAR_SOURCE };
    setWorkspaceSidebarSourceForTests(fake);
    expect(workspaceSidebarSource()).toBe(fake);
    setWorkspaceSidebarSourceForTests(null);
    expect(workspaceSidebarSource()).toBe(DEFAULT_WORKSPACE_SIDEBAR_SOURCE);
  });
});

describe("fileContentText", () => {
  it("reads UTF-8 text, and base64 bytes that are UTF-8", () => {
    expect(fileContentText(file("plain"))).toBe("plain");
    const encoded = btoa(String.fromCharCode(...new TextEncoder().encode("héllo")));
    expect(fileContentText(file(encoded, { encoding: "base64" }))).toBe("héllo");
  });

  it("is null for bytes that aren't text", () => {
    const bytes = btoa(String.fromCharCode(0xff, 0xfe, 0x00));
    expect(fileContentText(file(bytes, { encoding: "base64" }))).toBeNull();
    expect(fileContentText(file("%%%", { encoding: "base64" }))).toBeNull();
  });
});
