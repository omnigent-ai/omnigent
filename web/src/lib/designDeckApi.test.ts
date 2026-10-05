import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchFileContent } from "@/hooks/useFileContent";
import { writeFileContent } from "@/hooks/useWriteFileContent";
import * as workspaceFiles from "@/hooks/useWorkspaceChangedFiles";
import {
  fetchDesignDefault,
  fetchDesignSearch,
  fetchDesignIndex,
  fetchImportTarget,
  fetchKitIndicator,
  materializeOrgKit,
  reconcileDesignIndex,
  saveDesignDefault,
} from "./designDeckApi";
import { authenticatedFetch } from "./identity";
import { getSessionSlim } from "./sessionsApi";

const { requestWorkspaceFileSearch } = workspaceFiles;

vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/hooks/useWriteFileContent", () => ({ writeFileContent: vi.fn() }));
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
const writeMock = vi.mocked(writeFileContent);

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
  writeMock.mockReset();
});

describe("fetchDesignSearch", () => {
  it("searches the workspace for decks and wireframes and returns file paths", async () => {
    searchMock.mockResolvedValue(
      response(200, {
        object: "list",
        data: [
          entry("decks/q3.slides.html"),
          entry("wireframes/app.wireframe.html"),
          entry("decks", "directory"),
        ],
        has_more: false,
      }),
    );

    expect(await fetchDesignSearch("conv_a")).toEqual({
      status: "ok",
      paths: ["decks/q3.slides.html", "wireframes/app.wireframe.html"],
      truncated: false,
    });
    expect(searchMock).toHaveBeenCalledWith("conv_a", {
      query: ".html",
      include: "**/*.slides.html,**/*.wireframe.html",
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

    expect(await fetchDesignSearch("conv_a")).toEqual({
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

    expect(await fetchDesignSearch("conv_a")).toEqual({
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
          entry(".omnigent/design-system/templates/app.wireframe.html"),
          entry("q3.slides.html"),
        ],
        has_more: false,
      }),
    );
    expect(await fetchDesignSearch("conv_a")).toEqual({
      status: "ok",
      paths: ["q3.slides.html"],
      truncated: false,
    });
  });

  it.each([404, 503])("reads a %s as an unavailable workspace", async (status) => {
    searchMock.mockResolvedValue(response(status, { error: { code: "runner_unavailable" } }));
    expect(await fetchDesignSearch("conv_a")).toEqual({ status: "unavailable" });
  });

  it("throws on any other failure", async () => {
    searchMock.mockResolvedValue(new Response("boom", { status: 500, statusText: "Server Error" }));
    await expect(fetchDesignSearch("conv_a")).rejects.toThrow("500 Server Error");
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

  it("returns the indexed decks and wireframes", async () => {
    const wireframe = { ...indexed, path: "app.wireframe.html", kind: "wireframe" };
    fetchMock.mockResolvedValue(response(200, { object: "list", data: [indexed, wireframe] }));
    expect(await fetchDesignIndex()).toEqual([indexed, wireframe]);
    expect(fetchMock).toHaveBeenCalledWith("/v1/design/artifacts");
  });

  it("returns null when the server has no index, so the page falls back to the scan", async () => {
    fetchMock.mockResolvedValue(response(404, { detail: "not found" }));
    expect(await fetchDesignIndex()).toBeNull();
    fetchMock.mockResolvedValue(response(500));
    expect(await fetchDesignIndex()).toBeNull();
  });
});

describe("reconcileDesignIndex", () => {
  it("replaces the session's rows of each kind with the scan's paths of that kind", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await reconcileDesignIndex("conv a", ["q3.slides.html", "w/app.wireframe.html", "x.html"]);
    const put = (paths: string[], kind: string) => [
      "/v1/sessions/conv%20a/design-artifacts",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths, kind }),
      },
    ];
    expect(fetchMock.mock.calls).toEqual([
      put(["q3.slides.html"], "deck"),
      put(["w/app.wireframe.html"], "wireframe"),
    ]);
  });

  it("swallows failures; the index is best-effort", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    await expect(reconcileDesignIndex("conv_a", [])).resolves.toBeUndefined();
  });
});

describe("design default", () => {
  const system = { kind: "full", host_id: "h1", path: "/ds/brand", name: "Brand" };

  it("reads a stored system, none, or unset", async () => {
    fetchMock.mockResolvedValueOnce(response(200, { design_default: system }));
    expect(await fetchDesignDefault()).toEqual({
      kind: "full",
      hostId: "h1",
      path: "/ds/brand",
      name: "Brand",
    });
    fetchMock.mockResolvedValueOnce(response(200, { design_default: { kind: "none" } }));
    expect(await fetchDesignDefault()).toEqual({ kind: "none" });
    fetchMock.mockResolvedValueOnce(response(200, { design_default: null }));
    expect(await fetchDesignDefault()).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith("/v1/me/preferences/design-default");
  });

  it("reads an unavailable preference (flag off, older server) as unset", async () => {
    fetchMock.mockResolvedValue(response(404, { detail: "not found" }));
    expect(await fetchDesignDefault()).toBeNull();
  });

  it("saves a system in the wire shape and throws on failure", async () => {
    fetchMock.mockResolvedValue(response(200, { design_default: system }));
    await saveDesignDefault({ kind: "full", hostId: "h1", path: "/ds/brand", name: "Brand" });
    expect(fetchMock).toHaveBeenCalledWith("/v1/me/preferences/design-default", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ design_default: system }),
    });
    fetchMock.mockResolvedValue(response(422));
    await expect(saveDesignDefault({ kind: "none" })).rejects.toThrow("422");
  });
});

describe("materializeOrgKit", () => {
  const kitJson = JSON.stringify({
    name: "Acme",
    css: "layouts.css",
    logo: { src: "img/logo.svg" },
    fonts: {
      heading: { family: "Acme", src: "fonts/a.woff2" },
      body: { family: "Acme", src: "fonts/a.woff2" },
    },
  });
  const files: Record<string, string> = {
    "kit.json": kitJson,
    "layouts.css": ".x{}",
    "img/logo.svg": "<svg/>",
    "fonts/a.woff2": "wOF2",
  };
  const serve = (overrides: Record<string, Response> = {}) =>
    fetchMock.mockImplementation(async (url) => {
      const rel = decodeURIComponent(String(url).replace("/v1/design-kit/", ""));
      return overrides[rel] ?? new Response(files[rel] ?? "", { status: files[rel] ? 200 : 404 });
    });

  it("copies each file the kit uses once, writing kit.json last", async () => {
    serve();
    await materializeOrgKit("conv_a");
    expect(writeMock.mock.calls.map(([, path]) => path)).toEqual([
      ".omnigent/design-kit/layouts.css",
      ".omnigent/design-kit/img/logo.svg",
      ".omnigent/design-kit/fonts/a.woff2",
      ".omnigent/design-kit/kit.json",
    ]);
    for (const [conv, path, content, encoding] of writeMock.mock.calls) {
      expect(conv).toBe("conv_a");
      expect(encoding).toBe("base64");
      expect(atob(content)).toBe(files[path.replace(".omnigent/design-kit/", "")]);
    }
  });

  it("writes nothing when a file is missing or kit.json is invalid", async () => {
    serve({ "img/logo.svg": new Response("", { status: 404 }) });
    await expect(materializeOrgKit("conv_a")).rejects.toThrow("img/logo.svg");
    expect(writeMock.mock.calls.map(([, path]) => path)).not.toContain(
      ".omnigent/design-kit/kit.json",
    );
    writeMock.mockReset();
    serve({ "kit.json": new Response("{}") });
    await expect(materializeOrgKit("conv_a")).rejects.toThrow("name");
    expect(writeMock).not.toHaveBeenCalled();
  });

  it("refuses a file over the per-asset cap", async () => {
    serve({ "layouts.css": new Response(new Uint8Array(2 * 1024 * 1024 + 1)) });
    await expect(materializeOrgKit("conv_a")).rejects.toThrow("larger than 2 MB");
    expect(writeMock).not.toHaveBeenCalled();
  });
});
