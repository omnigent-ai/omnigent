import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type * as SandboxProfilesModule from "@/lib/sandboxProfilesApi";
import {
  fetchSandboxProfiles,
  SANDBOX_PROFILES_QUERY_KEY,
  type SandboxProfile,
  type SandboxProfilesResponse,
} from "@/lib/sandboxProfilesApi";
import { SandboxProfilePicker, type SandboxProfileSelection } from "./SandboxProfilePicker";

vi.mock("@/lib/sandboxProfilesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxProfilesModule>()),
  fetchSandboxProfiles: vi.fn(),
}));

beforeEach(() => vi.mocked(fetchSandboxProfiles).mockReset());
afterEach(cleanup);

const selected: SandboxProfile = {
  id: "large",
  name: "Large workspace",
  description: "For builds",
  provider: "kubernetes",
  revision: "revision-1",
  config: {},
  enabled: true,
};

function catalog(profile: SandboxProfile): SandboxProfilesResponse {
  return {
    profiles: [profile],
    providers: ["kubernetes"],
    can_manage: false,
    next_cursor: null,
  };
}

function mount(data?: SandboxProfilesResponse, value: SandboxProfileSelection | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (data) client.setQueryData(SANDBOX_PROFILES_QUERY_KEY, data);
  const onChange = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <SandboxProfilePicker provider="kubernetes" value={value} onChange={onChange} />
    </QueryClientProvider>,
  );
  return { client, onChange };
}

it("allows long profile names and descriptions to wrap in the constrained menu", () => {
  const profile = {
    ...selected,
    name: "Small development sandbox",
    description: "A small development sandbox for building and testing applications.",
  };
  const { onChange } = mount(catalog(profile));
  fireEvent.pointerDown(screen.getByRole("button", { name: "Sandbox profile: Default" }), {
    button: 0,
  });
  expect(screen.getByText(profile.name)).toHaveClass("whitespace-normal", "break-words");
  expect(screen.getByText(profile.description)).toHaveClass("whitespace-normal", "break-words");
  fireEvent.click(screen.getByRole("menuitem", { name: /^Small development sandbox/ }));
  expect(onChange).toHaveBeenCalledWith({
    id: profile.id,
    name: profile.name,
    provider: profile.provider,
    revision: profile.revision,
  });
});

it.each([
  { change: "edited", managed: false },
  { change: "disabled", managed: false },
  { change: "edited", managed: true },
  { change: "disabled", managed: true },
])(
  "keeps the selected revision after a profile is $change (managed=$managed)",
  async ({ change, managed }) => {
    const data = { ...catalog(selected), managed_providers: managed ? [selected.provider] : [] };
    const { client, onChange } = mount(data, selected);
    expect(screen.queryByText("(changed)")).toBeNull();
    const current = {
      ...selected,
      revision: "revision-2",
      enabled: change !== "disabled",
    };
    await act(async () => {
      client.setQueryData(SANDBOX_PROFILES_QUERY_KEY, { ...data, profiles: [current] });
    });
    expect(await screen.findByText("(changed)")).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Sandbox profile: Large workspace" }),
      {
        button: 0,
      },
    );
    expect(await screen.findByRole("status")).toHaveTextContent("Choose a profile again");
    if (change === "disabled") {
      expect(screen.queryByRole("menuitem", { name: /^Large workspace/ })).toBeNull();
      if (managed) {
        expect(screen.queryByRole("menuitem", { name: /^Default/ })).toBeNull();
      } else {
        fireEvent.click(screen.getByRole("menuitem", { name: /^Default/ }));
        expect(onChange).toHaveBeenCalledWith(null);
      }
    } else {
      fireEvent.click(screen.getByRole("menuitem", { name: /^Large workspace/ }));
      expect(onChange).toHaveBeenCalledWith({
        id: current.id,
        name: current.name,
        provider: current.provider,
        revision: current.revision,
      });
    }
  },
);

it.each([null, { ...selected, provider: "other" }])(
  "requires an explicit managed choice and scopes selections to the provider (%j)",
  (value) => {
    const { onChange } = mount(
      {
        ...catalog(selected),
        profiles: [
          { ...selected, provider: "other", name: "Other provider" },
          { ...selected, managed: true },
        ],
        managed_providers: [selected.provider],
      },
      value,
    );
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Sandbox profile: Choose profile" }), {
      button: 0,
    });
    expect(screen.getByRole("status")).toHaveTextContent("Choose a sandbox profile to continue");
    expect(screen.queryByRole("menuitem", { name: /^Default/ })).toBeNull();
    expect(screen.queryByText("Other provider")).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: /^Large workspace/ }));
    expect(onChange).toHaveBeenCalledWith({
      id: selected.id,
      name: selected.name,
      provider: selected.provider,
      revision: selected.revision,
    });
  },
);

it("shows catalog loading and a recoverable error without suggesting a default", async () => {
  let rejectCatalog!: (error: Error) => void;
  vi.mocked(fetchSandboxProfiles).mockImplementationOnce(
    () =>
      new Promise((_, reject) => {
        rejectCatalog = reject;
      }),
  );
  const { onChange } = mount();
  fireEvent.pointerDown(
    screen.getByRole("button", { name: "Sandbox profile: Loading profiles…" }),
    {
      button: 0,
    },
  );
  expect(screen.getByRole("status")).toHaveTextContent("Loading sandbox profiles");
  expect(screen.queryByRole("menuitem", { name: /^Default/ })).toBeNull();
  await act(async () => rejectCatalog(new Error("Unavailable")));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load sandbox profiles");
  expect(screen.getByText("Profiles unavailable")).toBeVisible();
  expect(screen.queryByRole("menuitem", { name: /^Default/ })).toBeNull();
  vi.mocked(fetchSandboxProfiles).mockResolvedValue({
    ...catalog(selected),
    managed_providers: [selected.provider],
  });
  fireEvent.click(screen.getByRole("menuitem", { name: "Try again" }));
  expect(
    await screen.findByRole("button", { name: "Sandbox profile: Choose profile" }),
  ).toBeVisible();
  expect(onChange).not.toHaveBeenCalled();
});

it.each([false, true])("shows an empty catalog (managed=%s)", (managed) => {
  const { onChange } = mount({
    ...catalog(selected),
    profiles: [],
    ...(managed ? { managed_providers: [selected.provider] } : {}),
  });
  fireEvent.pointerDown(
    screen.getByRole("button", { name: "Sandbox profile: No profiles available" }),
    {
      button: 0,
    },
  );
  expect(screen.getByRole("status")).toHaveTextContent(
    managed ? "No sandbox profiles available" : "No saved profiles",
  );
  expect(!!screen.queryByRole("menuitem", { name: /^Default/ })).toBe(!managed);
  expect(onChange).not.toHaveBeenCalled();
});
