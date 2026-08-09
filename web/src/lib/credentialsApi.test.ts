import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  connectGithub,
  disconnectGithub,
  listCredentials,
  listGithubRepos,
} from "./credentialsApi";
import { authenticatedFetch } from "./identity";

vi.mock("./identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const mockAuthenticatedFetch = vi.mocked(authenticatedFetch);

function mockJsonResponse(
  body: unknown,
  init: { ok?: boolean; status?: number; statusText?: string } = {},
): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.statusText ?? "OK",
    json: async () => body,
  } as unknown as Response;
}

beforeEach(() => {
  mockAuthenticatedFetch.mockReset();
});

describe("listCredentials", () => {
  it("GETs /v1/credentials and returns the credentials array", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse({
        credentials: [
          { provider: "github", login: "alice-gh", scopes: "repo", connected_at: 1_700_000_000 },
        ],
        enabled: true,
      }),
    );
    const result = await listCredentials();
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/credentials");
    expect(result).toEqual({
      ok: true,
      credentials: [
        { provider: "github", login: "alice-gh", scopes: "repo", connected_at: 1_700_000_000 },
      ],
      enabled: true,
    });
  });

  it("returns a typed failure on a non-OK response", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse(
        { error: { code: "unauthorized", message: "Authentication required" } },
        { ok: false, status: 401 },
      ),
    );
    const result = await listCredentials();
    expect(result).toEqual({
      ok: false,
      error: "Authentication required",
      status: 401,
      code: "Authentication required",
    });
  });
});

describe("connectGithub", () => {
  it("POSTs /v1/credentials/github/connect and returns the authorize_url", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse({ authorize_url: "https://github.com/login/oauth/authorize?client_id=x" }),
    );
    const result = await connectGithub();
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/credentials/github/connect", {
      method: "POST",
    });
    expect(result).toEqual({
      ok: true,
      authorize_url: "https://github.com/login/oauth/authorize?client_id=x",
    });
  });

  it("surfaces the credentials_disabled 409 with a matching code", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse(
        { error: { code: "conflict", message: "credentials_disabled" } },
        { ok: false, status: 409 },
      ),
    );
    const result = await connectGithub();
    expect(result).toEqual({
      ok: false,
      error: "credentials_disabled",
      status: 409,
      code: "credentials_disabled",
    });
  });
});

describe("disconnectGithub", () => {
  it("DELETEs /v1/credentials/github and returns ok", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse({ ok: true }));
    const result = await disconnectGithub();
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/credentials/github", {
      method: "DELETE",
    });
    expect(result).toEqual({ ok: true });
  });
});

describe("listGithubRepos", () => {
  it("GETs /v1/credentials/github/repos and returns the repos array", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse({
        repos: [
          {
            full_name: "alice/proj",
            clone_url: "https://github.com/alice/proj.git",
            default_branch: "main",
            private: false,
          },
        ],
      }),
    );
    const result = await listGithubRepos();
    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/credentials/github/repos");
    expect(result).toEqual({
      ok: true,
      repos: [
        {
          full_name: "alice/proj",
          clone_url: "https://github.com/alice/proj.git",
          default_branch: "main",
          private: false,
        },
      ],
    });
  });

  it("distinguishes github_not_connected from credentials_disabled via result.code", async () => {
    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse(
        { error: { code: "conflict", message: "github_not_connected" } },
        { ok: false, status: 409 },
      ),
    );
    const notConnected = await listGithubRepos();
    expect(notConnected).toEqual({
      ok: false,
      error: "github_not_connected",
      status: 409,
      code: "github_not_connected",
    });

    mockAuthenticatedFetch.mockResolvedValueOnce(
      mockJsonResponse(
        { error: { code: "conflict", message: "credentials_disabled" } },
        { ok: false, status: 409 },
      ),
    );
    const disabled = await listGithubRepos();
    expect(disabled).toEqual({
      ok: false,
      error: "credentials_disabled",
      status: 409,
      code: "credentials_disabled",
    });
  });

  it("returns NETWORK_FAILURE when the fetch itself rejects", async () => {
    mockAuthenticatedFetch.mockRejectedValueOnce(new Error("offline"));
    const result = await listGithubRepos();
    expect(result).toEqual({
      ok: false,
      error: "Could not reach the server. Check your connection.",
      status: 0,
    });
  });
});
