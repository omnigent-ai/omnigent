import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchFileContent } from "@/hooks/useFileContent";
import type * as workspaceFiles from "@/hooks/useWorkspaceChangedFiles";
import { requestWorkspaceFileSearch } from "@/hooks/useWorkspaceChangedFiles";
import { WORKSPACE_FILE_SEARCH_LIMIT, fetchDeckSearch, fetchKitIndicator } from "./designDeckApi";

vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importActual) => ({
  ...(await importActual<typeof workspaceFiles>()),
  requestWorkspaceFileSearch: vi.fn(),
}));

const searchMock = vi.mocked(requestWorkspaceFileSearch);
const contentMock = vi.mocked(fetchFileContent);

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

  it("treats a full page of results as truncated when the server omits the flag", async () => {
    const data = Array.from({ length: WORKSPACE_FILE_SEARCH_LIMIT }, (_, i) =>
      entry(`decks/d${i}.slides.html`),
    );
    searchMock.mockResolvedValue(
      response(200, { object: "list", data, has_more: false }),
    );

    const result = await fetchDeckSearch("conv_a");
    expect(result).toMatchObject({ status: "ok", truncated: true });
    expect(result.status === "ok" && result.paths).toHaveLength(WORKSPACE_FILE_SEARCH_LIMIT);
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

describe("fetchKitIndicator", () => {
  it("reads only kit.json and names the kit", async () => {
    contentMock.mockResolvedValue({
      object: "session.environment.filesystem.file_content",
      path: ".omnigent/design-kit/kit.json",
      content_type: "application/json",
      encoding: "utf-8",
      content: '{"name":"Acme","logo":{"src":"logo.svg"}}',
      bytes: 40,
    });

    expect(await fetchKitIndicator("conv_a")).toEqual({ status: "ok", name: "Acme" });
    expect(contentMock).toHaveBeenCalledTimes(1);
    expect(contentMock).toHaveBeenCalledWith("conv_a", ".omnigent/design-kit/kit.json");
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
