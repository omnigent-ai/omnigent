import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import type { SandboxProfile } from "@/lib/sandboxProfilesApi";
import { SettingsSandboxesSection } from "./SettingsSandboxesSection";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

let profiles: SandboxProfile[];
let canManage: boolean;
let providers: string[];
let managedProviders: string[];
let rejectSave: boolean;
const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

beforeEach(() => {
  profiles = [];
  canManage = true;
  providers = ["agent_sandbox"];
  managedProviders = [];
  rejectSave = false;
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
    if (init?.method === "PUT") {
      if (rejectSave)
        return reply({ error: { message: "Unsupported sandbox config field: unsafe" } }, 422);
      const profile = {
        ...JSON.parse(init.body as string),
        id: String(input).split("/").at(-1),
        revision: "revision-1",
      } as SandboxProfile;
      profiles = [...profiles.filter((item) => item.id !== profile.id), profile];
      return reply(profile);
    }
    if (init?.method === "DELETE") {
      profiles = profiles.map((profile) => ({ ...profile, enabled: false }));
      return reply(profiles[0]);
    }
    return reply({
      profiles,
      providers,
      managed_providers: managedProviders,
      can_manage: canManage,
      next_cursor: null,
    });
  });
});
afterEach(cleanup);

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SettingsSandboxesSection />
    </QueryClientProvider>,
  );
}

it("creates, edits, and disables a profile; rejects invalid JSON and surfaces server validation", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "New profile" }));
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Large workspace" } });
  fireEvent.change(screen.getByLabelText("Profile ID"), { target: { value: "large" } });
  fireEvent.change(screen.getByLabelText("Sandbox configuration"), { target: { value: "[]" } });
  expect(screen.getByRole("alert")).toHaveTextContent("must be a JSON object");
  expect(screen.getByRole("button", { name: "Save profile" })).toBeDisabled();
  const config = { resources: { limits: { cpu: "2", memory: "4Gi" } } };
  fireEvent.change(screen.getByLabelText("Sandbox configuration"), {
    target: { value: JSON.stringify(config) },
  });
  rejectSave = true;
  fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Unsupported sandbox config field");
  rejectSave = false;
  fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
  await screen.findByRole("button", { name: "Edit Large workspace" });
  expect(profiles[0]).toMatchObject({ id: "large", config, enabled: true });
  fireEvent.click(screen.getByRole("button", { name: "Edit Large workspace" }));
  expect(screen.getByLabelText("Profile ID")).toBeDisabled();
  expect(
    JSON.parse((screen.getByLabelText("Sandbox configuration") as HTMLTextAreaElement).value),
  ).toEqual(config);
  fireEvent.change(screen.getByLabelText("Description"), {
    target: { value: "For bigger builds" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
  await screen.findByText("For bigger builds");
  fireEvent.click(screen.getByRole("button", { name: "Disable Large workspace" }));
  await screen.findByText("Disabled");
  await waitFor(() => expect(profiles[0].enabled).toBe(false));
});

it("does not offer profile writes to a non-admin", async () => {
  canManage = false;
  mount();
  await screen.findByText("Only administrators can manage sandbox profiles.");
  expect(screen.queryByRole("button", { name: "New profile" })).toBeNull();
});

it("keeps provider-owned profiles read-only for admins alongside editable profiles with the same ID", async () => {
  const profile: SandboxProfile = {
    id: "large",
    name: "Local build",
    description: "",
    provider: "agent_sandbox",
    revision: "revision-1",
    config: {},
    enabled: true,
  };
  profiles = [profile, { ...profile, provider: "remote", name: "Remote build", managed: true }];
  managedProviders = ["remote"];
  mount();
  expect(await screen.findByRole("button", { name: "Edit Local build" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Disable Local build" })).toBeVisible();
  expect(screen.getByText("Remote build")).toBeVisible();
  expect(screen.getByText("Managed by provider")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Edit Remote build" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Disable Remote build" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "New profile" }));
  fireEvent.click(screen.getByRole("combobox"));
  expect(screen.getAllByRole("option")).toHaveLength(1);
});

it("does not promise default launches when a provider-managed catalog is empty", async () => {
  canManage = false;
  providers = [];
  managedProviders = ["remote"];
  mount();
  expect(await screen.findByText(/need an available profile before starting a chat/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "New profile" })).toBeNull();
  expect(screen.queryByText("Only administrators can manage sandbox profiles.")).toBeNull();
  expect(screen.queryByText(/Sessions use the server default/)).toBeNull();
});
