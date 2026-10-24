import { expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { fetchSandboxProfiles } from "./sandboxProfilesApi";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

it("loads the complete paginated catalog for the settings list and launch picker", async () => {
  const page = (profiles: unknown[], next_cursor: string | null) =>
    new Response(
      JSON.stringify({ profiles, next_cursor, providers: ["agent_sandbox"], can_manage: true }),
    );
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(page([{ id: "first" }], "first"))
    .mockResolvedValueOnce(page([{ id: "last" }], null));
  expect((await fetchSandboxProfiles()).profiles).toEqual([{ id: "first" }, { id: "last" }]);
  expect(authenticatedFetch).toHaveBeenLastCalledWith(
    "/v1/sandbox-profiles?limit=100&cursor=first",
    undefined,
  );
});
