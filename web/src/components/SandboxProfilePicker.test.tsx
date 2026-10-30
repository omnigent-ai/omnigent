import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useState, type ReactNode } from "react";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import type * as SandboxProfilesModule from "@/lib/sandboxProfilesApi";
import {
  fetchSandboxProfiles,
  SANDBOX_PROFILES_QUERY_KEY,
  type SandboxProfile,
} from "@/lib/sandboxProfilesApi";
import {
  SandboxProfilePicker,
  useSandboxProfileCatalog,
  type SandboxProfileSelection,
} from "./SandboxProfilePicker";

const capability = vi.hoisted(() => ({ info: "loading" as ServerInfo | "loading" }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => capability.info }));
vi.mock("@/lib/sandboxProfilesApi", async (original) => ({
  ...(await original<typeof SandboxProfilesModule>()),
  fetchSandboxProfiles: vi.fn(),
}));
const rows: SandboxProfile[] = Array.from({ length: 35 }, (_, i) => ({
  id: `profile-${i}`,
  name: `Build profile ${i}`,
  description:
    "An approved development environment with a long description for large project builds.",
  provider: "company",
  revision: "v1",
}));
let available: SandboxProfile[];
beforeEach(() => {
  capability.info = {
    ...FALLBACK_SERVER_INFO,
    managed_sandboxes_enabled: true,
    features: { sandbox_profiles: true },
  };
  available = rows;
  vi.mocked(fetchSandboxProfiles)
    .mockReset()
    .mockImplementation(async (_provider, options) => {
      const found = available.filter(
        (p) =>
          (!options?.profileId || p.id === options.profileId) &&
          (!options?.search || p.name.includes(options.search)),
      );
      return {
        profiles: found.slice(0, 20),
        managed_providers: ["company"],
        next_cursor: found.length > 20 ? "more" : null,
      };
    });
});
afterEach(cleanup);
function mount(value: SandboxProfileSelection | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onChange = vi.fn();
  function Picker() {
    const [selection, setSelection] = useState(value);
    const { launchError } = useSandboxProfileCatalog("company", selection);
    return (
      <>
        <SandboxProfilePicker
          provider="company"
          value={selection}
          onChange={(p) => {
            onChange(p);
            setSelection(p);
          }}
        />
        <button type="button" disabled={launchError !== null}>
          Start
        </button>
      </>
    );
  }
  render(
    <QueryClientProvider client={client}>
      <Picker />
    </QueryClientProvider>,
  );
  return { client, onChange };
}
async function open(name = "Choose profile") {
  fireEvent.click(await screen.findByRole("button", { name: `Sandbox profile: ${name}` }));
  return screen.findByRole("combobox", { name: "Search sandbox profiles" });
}
it("caps the menu and finds profiles beyond the first page", async () => {
  const { onChange } = mount();
  const input = await open();
  expect(screen.getAllByRole("option")).toHaveLength(20);
  expect(screen.getByText(/Showing first 20/)).toBeVisible();
  fireEvent.change(input, { target: { value: "profile 34" } });
  const result = await screen.findByRole("option", { name: /^Build profile 34/ });
  expect(screen.getAllByRole("option")).toHaveLength(1);
  expect(screen.getByText(rows[34].name)).toHaveClass("truncate");
  fireEvent.click(result);
  expect(onChange).toHaveBeenCalledWith(rows[34]);
  expect(
    await screen.findByRole("button", { name: "Sandbox profile: Build profile 34" }),
  ).toBeVisible();
});
it("keeps an out-of-page selection when search has no matches", async () => {
  const { onChange } = mount(rows[34]);
  const input = await open(rows[34].name);
  fireEvent.change(input, { target: { value: "does not exist" } });
  expect(await screen.findByText("No matching profiles.")).toBeVisible();
  expect(screen.queryByText("(changed)")).toBeNull();
  expect(onChange).not.toHaveBeenCalled();
});
it("allows launching after selecting a new revision from search with an older lookup cached", async () => {
  const { onChange } = mount(rows[0]);
  await waitFor(() => expect(screen.getByRole("button", { name: "Start" })).toBeEnabled());
  const revised = { ...rows[0], revision: "v2" };
  available = [revised];
  const input = await open(rows[0].name);
  fireEvent.change(input, { target: { value: rows[0].name } });
  const result = await screen.findByRole("option", { name: /^Build profile 0/ });
  expect(onChange).not.toHaveBeenCalled();
  fireEvent.click(result);
  expect(onChange).toHaveBeenCalledWith(revised);
  await waitFor(() => expect(screen.getByRole("button", { name: "Start" })).toBeEnabled());
  expect(screen.queryByText("(changed)")).toBeNull();
});
it.each(["renamed", "revised", "revoked"])(
  "revalidates the selected profile when %s",
  async (change) => {
    const { client, onChange } = mount(rows[34]);
    await open(rows[34].name);
    available =
      change === "revoked"
        ? []
        : [{ ...rows[34], name: "Renamed build", revision: change === "revised" ? "v2" : "v1" }];
    await act(() => client.invalidateQueries({ queryKey: SANDBOX_PROFILES_QUERY_KEY }));
    if (change === "renamed")
      expect(
        await screen.findByRole("button", { name: "Sandbox profile: Renamed build" }),
      ).toBeVisible();
    else expect(await screen.findByText("(changed)")).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
  },
);
it("shows a recoverable provider error", async () => {
  vi.mocked(fetchSandboxProfiles).mockRejectedValue(new Error("unavailable"));
  mount();
  await open("Profiles unavailable");
  expect(screen.getByRole("alert")).toHaveTextContent("Could not load sandbox profiles");
  vi.mocked(fetchSandboxProfiles).mockResolvedValue({
    profiles: [rows[0]],
    managed_providers: ["company"],
    next_cursor: null,
  });
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(await screen.findByRole("option", { name: /^Build profile 0/ })).toBeVisible();
});
it("distinguishes no access from no search matches", async () => {
  available = [];
  mount();
  await open("No profiles available");
  expect(screen.getByRole("status")).toHaveTextContent("No sandbox profiles available");
});
it.each(["loading", "missing", "disabled", "ordinary", "managed-disabled"])(
  "preserves legacy behavior for %s",
  async (state) => {
    capability.info =
      state === "loading"
        ? "loading"
        : {
            ...FALLBACK_SERVER_INFO,
            managed_sandboxes_enabled: state !== "managed-disabled",
            features:
              state === "missing"
                ? {}
                : { sandbox_profiles: state === "ordinary" || state === "managed-disabled" },
          };
    vi.mocked(fetchSandboxProfiles).mockResolvedValue({
      profiles: [],
      managed_providers: [],
      next_cursor: null,
    });
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result, unmount } = renderHook(() => useSandboxProfileCatalog("company", rows[0]), {
      wrapper,
    });
    if (state === "ordinary")
      await waitFor(() => expect(result.current.query.isSuccess).toBe(true));
    expect(result.current.selection).toBeNull();
    expect(result.current.launchError).toBeNull();
    if (state !== "ordinary") expect(fetchSandboxProfiles).not.toHaveBeenCalled();
    else {
      const cached = result.current.query.data;
      unmount();
      client.setQueryData([...SANDBOX_PROFILES_QUERY_KEY, "company"], cached, {
        updatedAt: Date.now() - 31_000,
      });
      let rejectRefresh!: (reason: Error) => void;
      vi.mocked(fetchSandboxProfiles).mockImplementation(
        () =>
          new Promise((_resolve, reject) => {
            rejectRefresh = reject;
          }),
      );
      const remounted = renderHook(() => useSandboxProfileCatalog("company", rows[0]), { wrapper });
      await waitFor(() => expect(remounted.result.current.query.isFetching).toBe(true));
      expect(remounted.result.current.launchError).toBeNull();
      await act(async () => rejectRefresh(new Error("Catalog unavailable")));
      await waitFor(() => expect(remounted.result.current.query.isError).toBe(true));
      expect(remounted.result.current.selection).toBeNull();
      expect(remounted.result.current.launchError).toBeNull();
    }
  },
);

it("selects a searched profile with the keyboard", async () => {
  const { onChange } = mount();
  const input = await open();
  fireEvent.change(input, { target: { value: "profile 34" } });
  await screen.findByRole("option", { name: /^Build profile 34/ });
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(onChange).toHaveBeenCalledWith(rows[34]));
});
