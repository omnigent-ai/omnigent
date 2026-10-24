import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { sessionPullRequestsKey, useUnlinkSessionPr } from "./useSessionPullRequests";

afterEach(() => vi.unstubAllGlobals());

const one = "https://github.com/example/one/pull/42";
const two = {
  url: "https://github.com/example/two/pull/7",
  host: "github.com",
  repository: "example/two",
  number: 7,
  relationship: "attached",
};

it.each([
  { server: "current", deleteStatus: 200 },
  { server: "older", deleteStatus: 404 },
])("unlinks on the $server server and stores what remains", async ({ deleteStatus }) => {
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new URL(String(input), "http://localhost");
      requests.push(`${init?.method ?? "GET"} ${request.pathname}`);
      if (init?.method === "DELETE") {
        expect(request.searchParams.get("url")).toBe(one);
        return deleteStatus === 200
          ? new Response(JSON.stringify({ object: "list", data: [two] }), { status: 200 })
          : new Response(JSON.stringify({ detail: "Not Found" }), { status: deleteStatus });
      }
      expect(JSON.parse(String(init?.body))).toEqual({ url: one, action: "remove" });
      return new Response(
        JSON.stringify({ object: "session.github.info", available: true, prs: [two] }),
        { status: 200 },
      );
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => useUnlinkSessionPr("conv_1"), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });

  result.current.mutate(one);

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual([two]);
  expect(client.getQueryData(sessionPullRequestsKey("conv_1"))).toEqual([two]);
  expect(requests).toEqual(
    deleteStatus === 200
      ? ["DELETE /v1/sessions/conv_1/pull-requests"]
      : [
          "DELETE /v1/sessions/conv_1/pull-requests",
          "POST /v1/sessions/conv_1/resources/github/prs",
        ],
  );
});
