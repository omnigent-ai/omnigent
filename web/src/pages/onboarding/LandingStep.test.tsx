import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LandingStep } from "./LandingStep";

afterEach(cleanup);

function renderLanding(over: Partial<Parameters<typeof LandingStep>[0]> = {}) {
  const props = {
    managedServers: [] as string[],
    recentServers: [] as string[],
    onGetStarted: vi.fn(),
    onJoinServer: vi.fn(),
    onJoinManaged: vi.fn(),
    onJoinUrl: vi.fn(),
    ...over,
  };
  render(<LandingStep {...props} />);
  return props;
}

function openDropdown() {
  fireEvent.pointerDown(screen.getByRole("button", { name: /choose team url/i }), { button: 0 });
}

describe("LandingStep", () => {
  it("fires onGetStarted / onJoinServer without presets", () => {
    const props = renderLanding();

    fireEvent.click(screen.getByRole("button", { name: /get started locally/i }));
    expect(props.onGetStarted).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: /join your team/i }));
    expect(props.onJoinServer).toHaveBeenCalledOnce();
  });

  it("makes the first preset's split button the only CTA", () => {
    const props = renderLanding({ managedServers: ["https://team.example.com/omnigent?o=1"] });

    // Primary button names the first preset's capitalized host label.
    fireEvent.click(screen.getByRole("button", { name: /join your team \(team\)/i }));
    expect(props.onJoinManaged).toHaveBeenCalledWith("https://team.example.com/omnigent?o=1");
    expect(screen.queryByRole("button", { name: /get started locally/i })).not.toBeInTheDocument();
  });

  it("names presets from the organization's server names, else by host", () => {
    renderLanding({
      managedServers: ["https://dbc-1.cloud.example.com/?o=1", "https://two.example.com/"],
      managedServerNames: { "https://dbc-1.cloud.example.com/?o=1": "Engineering" },
    });
    expect(
      screen.getByRole("button", { name: /join your team \(engineering\)/i }),
    ).toBeInTheDocument();
    openDropdown();
    // An unnamed preset keeps its URL.
    expect(screen.getByRole("menuitem", { name: "two.example.com" })).toBeInTheDocument();
  });

  it("names recents that named themselves, keeping the host", () => {
    renderLanding({
      managedServers: ["https://team.example.com/"],
      recentServers: ["https://omni.example/", "https://plain.example/"],
      serverNames: {
        "https://omni.example": "Acme Engineering",
        "https://team.example.com": "Not shown",
      },
    });
    // The organization's preset keeps its own label.
    expect(screen.getByRole("button", { name: /join your team \(team\)/i })).toBeInTheDocument();
    openDropdown();
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toEqual([
      "Acme Engineering (omni.example)",
      "plain.example",
    ]);
  });

  it("lists the other presets, then recents, in the dropdown", () => {
    const props = renderLanding({
      managedServers: ["https://team.example.com", "https://other.example.com/"],
      recentServers: ["https://old.example.com/"],
    });
    openDropdown();
    // The first preset is the main button, so it isn't repeated here.
    expect(screen.getAllByRole("menuitem").map((i) => i.textContent)).toEqual([
      "other.example.com",
      "old.example.com",
    ]);
    fireEvent.click(screen.getByRole("menuitem", { name: "other.example.com" }));
    expect(props.onJoinManaged).toHaveBeenCalledWith("https://other.example.com/");

    openDropdown();
    fireEvent.click(screen.getByRole("menuitem", { name: "old.example.com" }));
    expect(props.onJoinUrl).toHaveBeenCalledWith("https://old.example.com/");
  });

  it("joins a URL typed into the dropdown, and rejects an invalid one", () => {
    const props = renderLanding({ managedServers: ["https://team.example.com"] });
    openDropdown();
    const input = screen.getByLabelText("Server URL");

    fireEvent.change(input, { target: { value: "ftp://nope" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByRole("alert")).toHaveTextContent(/valid http\(s\) server url/i);
    expect(props.onJoinUrl).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "https://typed.example.com/x" } });
    fireEvent.click(screen.getByRole("button", { name: "Join server" }));
    expect(props.onJoinUrl).toHaveBeenCalledWith("https://typed.example.com/");
  });

  it("shows a connect error above the preset CTA", () => {
    renderLanding({ managedServers: ["https://team.example.com"], error: "boom" });
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
  });
});
