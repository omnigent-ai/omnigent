import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import {
  SANDBOX_PROFILES_QUERY_KEY,
  type SandboxProfile,
  type SandboxProfilesResponse,
} from "@/lib/sandboxProfilesApi";
import { SandboxProfilePicker } from "./SandboxProfilePicker";

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

it.each(["edited", "disabled"])(
  "keeps the selected revision until the user chooses again after a profile is %s",
  async (change) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(SANDBOX_PROFILES_QUERY_KEY, catalog(selected));
    const onChange = vi.fn();
    render(
      <QueryClientProvider client={client}>
        <SandboxProfilePicker provider="kubernetes" value={selected} onChange={onChange} />
      </QueryClientProvider>,
    );
    expect(screen.queryByText("(changed)")).toBeNull();
    const current = {
      ...selected,
      revision: "revision-2",
      enabled: change !== "disabled",
    };
    await act(async () => {
      client.setQueryData(SANDBOX_PROFILES_QUERY_KEY, catalog(current));
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
      fireEvent.click(screen.getByRole("menuitem", { name: /^Default/ }));
      expect(onChange).toHaveBeenCalledWith(null);
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
