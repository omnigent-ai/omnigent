import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchFileContent } from "@/hooks/useFileContent";
import * as workspaceFiles from "@/hooks/useWorkspaceChangedFiles";
import {
  fetchDeckSearch,
  fetchDesignIndex,
  fetchImportTarget,
  fetchKitIndicator,
  reconcileDesignIndex,
} from "./designDeckApi";
import { authenticatedFetch } from "./identity";
import { getSessionSlim } from "./sessionsApi";

const { requestWorkspaceFileSearch } = workspaceFiles;

vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("./sessionsApi", () => ({ getSessionSlim: vi.fn() }));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importActual) => ({
  ...(await importActual<typeof workspaceFiles>()),
  requestWorkspaceFileSearch: vi.fn(),
}));

const searchMock = vi.mocked(requestWorkspaceFileSearch);
const contentMock = vi.mocked(fetchFileContent);
const fetchMock = vi.mocked(authenticatedFetch);
const sessionMock = vi.mocked(getSessionSlim);

function response(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function entry(path: string, type = "file") {
  return { id: path, name: path.split("/").at(-1), path, type, bytes: 1, modified_at: 1 };
}

beforeEach(() => {
  searchMock.mockReset();
  contentMock.mockReset();
  fetchMock.mockReset();
});

describe("fetchDeckSearch", () => {
  it("searches the workspace for slide decks and returns file paths", async () => {
    searchMock.mockResolvedValue(
      response(200, {
        object: "list",
        data: [entry("decks/q3.slides.html"), entry("decks", "directory")],
        has_more: false,
      }),
    );

    expect(await fetchDeckSearch("conv_a")).toEqual({
      status: "ok",
      paths: ["decks/q3.slides.html"],
      truncated: false,
    });
    expect(searchMock).toHaveBeenCalledWith("conv_a", {
      query: ".slides.html",
      include: "**/*.slides.html",
    });
  });

  it("surfaces the server truncated flag", async () => {
    searchMock.mockResolvedValue(
      response(200, {
        object: "list",
        data: [entry("decks/q3.slides.html")],
        has_more: false,
        truncated: true,
      }),
    );

    expect(await fetchDeckSearch("conv_a")).toEqual({
      status: "ok",
      paths: ["decks/q3.slides.html"],
      truncated: true,
    });
  });

  it("treats has_more as truncated even for a small result page", async () => {
    searchMock.mockResolvedValue(
      response(200, {
        object: "list",
        data: [entry("decks/q3.slides.html")],
        has_more: true,
        truncated: false,
      }),
    );

    expect(await fetchDeckSearch("conv_a")).toEqual({
      status: "ok",
      paths: ["decks/q3.slides.html"],
      truncated: true,
    });
  });

  it("leaves out decks inside an imported design system", async () => {
    searchMock.mockResolvedValue(
      response(200, {
        object: "list",
        data: [
          entry(".omnigent/design-system/slides/title.slides.html"),
          entry("decks/.omnigent/design-system/x.slides.html"),
          entry("q3.slides.html"),
        ],
        has_more: false,
      }),
    );
    expect(await fetchDeckSearch("conv_a")).toEqual({
      status: "ok",
      paths: ["q3.slides.html"],
      truncated: false,
    });
  });

  it.each([404, 503])("reads a %s as an unavailable workspace", async (status) => {
    searchMock.mockResolvedValue(response(status, { error: { code: "runner_unavailable" } }));
    expect(await fetchDeckSearch("conv_a")).toEqual({ status: "unavailable" });
  });

  it("throws on any other failure", async () => {
    searchMock.mockResolvedValue(new Response("boom", { status: 500, statusText: "Server Error" }));
    await expect(fetchDeckSearch("conv_a")).rejects.toThrow("500 Server Error");
  });
});

describe("fetchImportTarget", () => {
  const session = (permissionLevel: number | null, hostId: string | null = "host_1") =>
    sessionMock.mockResolvedValue({ permissionLevel, hostId } as Awaited<
      ReturnType<typeof getSessionSlim>
    >);
  const pointer = (path: string) =>
    contentMock.mockResolvedValue({
      object: "session.environment.filesystem.file_content",
      path: ".omnigent/design-system.json",
      content_type: "application/json",
      encoding: "utf-8",
      content: JSON.stringify({ path, kind: "full", name: "Acme" }),
      bytes: 10,
    });

  it("offers the owner an outside system on the session's host", async () => {
    session(null);
    pointer("/brand/acme");
    expect(await fetchImportTarget("conv_a")).toEqual({
      hostId: "host_1",
      source: { path: "/brand/acme", kind: "full", name: "Acme" },
    });
  });

  it.each([
    ["an imported system", () => (session(null), pointer(".omnigent/design-system"))],
    ["a viewer who is not the owner", () => (session(1), pointer("/brand/acme"))],
    ["a session without a host", () => (session(null, null), pointer("/brand/acme"))],
    ["no pointer", () => (session(null), contentMock.mockRejectedValue(new Error("404")))],
  ])("offers nothing for %s", async (_label, arrange) => {
    arrange();
    expect(await fetchImportTarget("conv_a")).toBeNull();
  });
});

describe("fetchKitIndicator", () => {
  const json = (path: string, content: string) => ({
    object: "session.environment.filesystem.file_content" as const,
    path,
    content_type: "application/json",
    encoding: "utf-8" as const,
    content,
    bytes: content.length,
  });
  const serve = (files: Record<string, string>) =>
    contentMock.mockImplementation(async (_id, path) => {
      if (files[path] === undefined) throw new Error("404 Not Found");
      return json(path, files[path]);
    });

  it("reads only the pointer and kit.json and names the kit", async () => {
    serve({ ".omnigent/design-kit/kit.json": '{"name":"Acme","logo":{"src":"logo.svg"}}' });

    expect(await fetchKitIndicator("conv_a")).toEqual({ status: "ok", name: "Acme" });
    expect(contentMock.mock.calls).toEqual([
      ["conv_a", ".omnigent/design-system.json"],
      ["conv_a", ".omnigent/design-kit/kit.json"],
    ]);
  });

  it("names a design system in place of the kit", async () => {
    serve({
      ".omnigent/design-system.json": '{"path":"/brand","kind":"skill","name":"Brand"}',
      ".omnigent/design-kit/kit.json": '{"name":"Acme"}',
    });
    expect(await fetchKitIndicator("conv_a")).toEqual({
      status: "system",
      name: "Brand",
      kind: "skill",
    });
    expect(contentMock).toHaveBeenCalledTimes(1);
  });

  it("is an invalid design system when the pointer does not parse", async () => {
    serve({ ".omnigent/design-system.json": '{"path":"/brand","kind":"kit"}' });
    expect(await fetchKitIndicator("conv_a")).toEqual({
      status: "invalid",
      reason: '"kind" must be "full" or "skill"',
      system: true,
    });
  });

  it("is none when kit.json does not exist", async () => {
    contentMock.mockRejectedValue(new Error("404 Not Found"));
    expect(await fetchKitIndicator("conv_a")).toEqual({ status: "none" });
  });

  it("is invalid when kit.json cannot be read", async () => {
    contentMock.mockRejectedValue(new Error("500 Server Error"));
    expect(await fetchKitIndicator("conv_a")).toEqual({
      status: "invalid",
      reason: "500 Server Error",
    });
  });
});

describe("fetchDesignIndex", () => {
  const indexed = {
    session_id: "conv_a",
    path: "q3.slides.html",
    kind: "deck",
    updated_at: 5,
    session_title: "Review",
    workspace: "/work/a",
  };

  it("returns the indexed decks", async () => {
    fetchMock.mockResolvedValue(response(200, { object: "list", data: [indexed] }));
    expect(await fetchDesignIndex()).toEqual([indexed]);
    expect(fetchMock).toHaveBeenCalledWith("/v1/design/artifacts?kind=deck");
  });

  it("returns null when the server has no index, so the page falls back to the scan", async () => {
    fetchMock.mockResolvedValue(response(404, { detail: "not found" }));
    expect(await fetchDesignIndex()).toBeNull();
    fetchMock.mockResolvedValue(response(500));
    expect(await fetchDesignIndex()).toBeNull();
  });
});

describe("reconcileDesignIndex", () => {
  it("replaces the session's deck rows with the scan's paths", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await reconcileDesignIndex("conv a", ["q3.slides.html"]);
    expect(fetchMock).toHaveBeenCalledWith("/v1/sessions/conv%20a/design-artifacts", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: ["q3.slides.html"], kind: "deck" }),
    });
  });

  it("swallows failures; the index is best-effort", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    await expect(reconcileDesignIndex("conv_a", [])).resolves.toBeUndefined();
  });
});
