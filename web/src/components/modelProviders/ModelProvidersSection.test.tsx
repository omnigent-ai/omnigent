// Tests for the ModelProviders settings section: providers and agent specs
// render from the host hooks, the add-provider form builds an entry through
// the upsert mutation, the pin dialog submits the agent's pin, and the Test
// button fires the probe mutation. Host hooks and mutations are mocked.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ModelProvidersSection } from "./ModelProvidersSection";
import * as hostsHook from "@/hooks/useHosts";
import * as hooks from "@/hooks/useHostProviders";
import type { HostProvider } from "@/lib/hostProvidersApi";

vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useHostProviders", () => ({
  useHostProviders: vi.fn(),
  useHostAgentSpecs: vi.fn(),
  useUpsertHostProvider: vi.fn(),
  useDeleteHostProvider: vi.fn(),
  useTestHostProvider: vi.fn(),
  usePinHostAgent: vi.fn(),
  useClearHostAgentPin: vi.fn(),
}));

const HOST = { host_id: "host_1", name: "laptop", owner: "me", status: "online" } as const;

const PROVIDERS: HostProvider[] = [
  {
    name: "openrouter",
    kind: "gateway",
    default: true,
    openai: {
      base_url: "https://openrouter.ai/api/v1",
      api_key_set: true,
      wire_api: "chat",
      models: { default: "gpt-x", alt: "gpt-y" },
    },
  },
];

const AGENTS = [
  {
    name: "my-agent",
    harness: "pi",
    model: null,
    auth: { type: "provider", name: "openrouter" },
    spec_version: 1,
    path: "/home/u/.omnigent/agents/my-agent/config.yaml",
  },
];

let upsertMutate: ReturnType<typeof vi.fn>;
let pinMutate: ReturnType<typeof vi.fn>;
let testMutate: ReturnType<typeof vi.fn>;

function mockQueries() {
  vi.mocked(hooks.useHostProviders).mockReturnValue({
    data: PROVIDERS,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useHostProviders>);
  vi.mocked(hooks.useHostAgentSpecs).mockReturnValue({
    data: AGENTS,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useHostAgentSpecs>);
}

beforeEach(() => {
  upsertMutate = vi.fn();
  pinMutate = vi.fn();
  testMutate = vi.fn();
  vi.mocked(hostsHook.useHosts).mockReturnValue({
    data: [HOST],
    isLoading: false,
  } as unknown as ReturnType<typeof hostsHook.useHosts>);
  mockQueries();
  vi.mocked(hooks.useUpsertHostProvider).mockReturnValue({
    mutate: upsertMutate,
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useUpsertHostProvider>);
  vi.mocked(hooks.useDeleteHostProvider).mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useDeleteHostProvider>);
  vi.mocked(hooks.useTestHostProvider).mockReturnValue({
    mutate: testMutate,
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useTestHostProvider>);
  vi.mocked(hooks.usePinHostAgent).mockReturnValue({
    mutate: pinMutate,
    isPending: false,
  } as unknown as ReturnType<typeof hooks.usePinHostAgent>);
  vi.mocked(hooks.useClearHostAgentPin).mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useClearHostAgentPin>);
});

afterEach(() => cleanup());

describe("ModelProvidersSection", () => {
  it("renders the host's providers and agent specs", () => {
    render(<ModelProvidersSection />);
    expect(screen.getByText("openrouter")).toBeTruthy();
    expect(screen.getByText("https://openrouter.ai/api/v1")).toBeTruthy();
    expect(screen.getByText("my-agent")).toBeTruthy();
    expect(screen.getByText(/provider: openrouter/)).toBeTruthy();
  });

  it("submits a built entry through the upsert mutation", async () => {
    render(<ModelProvidersSection />);
    fireEvent.click(screen.getByText("Add provider"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "9router" } });
    fireEvent.click(screen.getByText("Add", { exact: true }));
    await waitFor(() => expect(upsertMutate).toHaveBeenCalledTimes(1));
    const call = upsertMutate.mock.calls[0][0];
    expect(call.name).toBe("9router");
    expect(call.entry.kind).toBe("gateway");
    expect(call.entry.openai).toBeTruthy();
  });

  it("submits the pin through the pin mutation", async () => {
    render(<ModelProvidersSection />);
    const pinButtons = screen.getAllByText("Pin");
    // one Pin button per agent row
    expect(pinButtons.length).toBe(1);
    fireEvent.click(pinButtons[0]);
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "gpt-x" } });
    fireEvent.click(screen.getByText("Save pin"));
    await waitFor(() => expect(pinMutate).toHaveBeenCalledTimes(1));
    const call = pinMutate.mock.calls[0][0];
    expect(call.agent).toBe("my-agent");
    expect(call.pin.model).toBe("gpt-x");
  });

  it("fires the provider probe from the Test button", () => {
    render(<ModelProvidersSection />);
    fireEvent.click(screen.getByText("Test"));
    expect(testMutate).toHaveBeenCalledWith(
      "openrouter",
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });
});
