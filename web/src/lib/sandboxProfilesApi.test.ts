import { beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { fetchSandboxProfiles } from "./sandboxProfilesApi";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

beforeEach(() => vi.mocked(authenticatedFetch).mockReset());

it("requests a capped provider-scoped search without fetching further pages", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(
      JSON.stringify({
        profiles: [{ id: "build" }],
        managed_providers: ["company"],
        next_cursor: "more",
      }),
    ),
  );
  const result = await fetchSandboxProfiles("company", { search: "large build" });
  expect(result.next_cursor).toBe("more");
  expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  const [url] = vi.mocked(authenticatedFetch).mock.calls[0];
  const params = new URL(String(url), "https://test").searchParams;
  expect(params.get("limit")).toBe("20");
  expect(params.get("provider")).toBe("company");
  expect(params.get("search")).toBe("large build");
});

it("looks up the selected profile independently of search results", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(
      JSON.stringify({ profiles: [], managed_providers: ["company"], next_cursor: null }),
    ),
  );
  await fetchSandboxProfiles("company", { profileId: "outside-page" });
  expect(String(vi.mocked(authenticatedFetch).mock.calls[0][0])).toContain(
    "profile_id=outside-page",
  );
});

it.each([
  [{ error: { message: "Provider unavailable" } }, "Provider unavailable"],
  [{ detail: "Choose another profile" }, "Choose another profile"],
  [{ detail: [{ message: "not a public message" }] }, "Sandbox profile request failed (503)."],
  ["invalid JSON", "Sandbox profile request failed (503)."],
])("extracts a useful error from %j", async (body, message) => {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(
    new Response(typeof body === "string" ? body : JSON.stringify(body), { status: 503 }),
  );
  await expect(fetchSandboxProfiles("company")).rejects.toThrow(message);
});
