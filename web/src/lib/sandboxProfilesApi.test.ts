import { expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { fetchSandboxProfiles } from "./sandboxProfilesApi";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

it.each([undefined, ["remote"]])(
  "loads the catalog with managed providers %j",
  async (managedProviders) => {
    const page = (profiles: unknown[], next_cursor: string | null) =>
      new Response(
        JSON.stringify({
          profiles,
          next_cursor,
          providers: ["agent_sandbox"],
          managed_providers: managedProviders,
          can_manage: true,
        }),
      );
    const local = { id: "large", provider: "agent_sandbox" };
    const managed = { id: "large", provider: "remote", managed: true, config: {} };
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(page([local], "first"))
      .mockResolvedValueOnce(page([managed], null));
    const result = await fetchSandboxProfiles();
    expect(result.profiles).toEqual([local, managed]);
    expect(result.providers).toEqual(["agent_sandbox"]);
    expect(result.managed_providers).toEqual(managedProviders);
    expect(result.can_manage).toBe(true);
    expect(result.next_cursor).toBeNull();
    expect(authenticatedFetch).toHaveBeenLastCalledWith(
      "/v1/sandbox-profiles?limit=100&cursor=first",
      undefined,
    );
  },
);
