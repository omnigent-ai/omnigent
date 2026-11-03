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

    // Primary button names the first preset's host label.
    fireEvent.click(screen.getByRole("button", { name: "Join your team (team)" }));
    expect(props.onJoinManaged).toHaveBeenCalledWith("https://team.example.com/omnigent?o=1");
    expect(screen.queryByRole("button", { name: /get started locally/i })).not.toBeInTheDocument();
  });

  it("names presets from the organization's server names, else by host", () => {
    renderLanding({
      managedServers: ["https://dbc-1.cloud.example.com/?o=1", "https://two.example.com/"],
      managedServerNames: { "https://dbc-1.cloud.example.com/?o=1": "Engineering" },
      serverNames: { "https://dbc-1.cloud.example.com": "Server's own name" },
      recentServers: ["https://dbc-1.cloud.example.com/?o=1"],
    });
    expect(
      screen.getByRole("button", { name: /join your team \(engineering\)/i }),
    ).toBeInTheDocument();
    openDropdown();
    expect(
      screen.getByRole("menuitemradio", {
        name: "Engineering (dbc-1.cloud.example.com)",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("(dbc-1.cloud.example.com)")).toHaveClass("text-muted-foreground");
    expect(
      screen.getByRole("menuitemradio", { name: "two (two.example.com)" }),
    ).toBeInTheDocument();
    expect(screen.getByText("(two.example.com)")).toHaveClass("text-muted-foreground");
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(2);
    expect(screen.queryByText("Recent servers")).not.toBeInTheDocument();
  });

  it("names recents that named themselves, keeping the host", () => {
    renderLanding({
      managedServers: ["https://team.example.com/"],
      recentServers: [
        "https://omni.example/",
        "https://plain.example/",
        "https://team.example.com/",
      ],
      serverNames: {
        "https://omni.example": "Acme Engineering",
        "https://team.example.com": "Not shown",
      },
    });
    // The organization's preset keeps its own label.
    expect(screen.getByRole("button", { name: "Join your team (team)" })).toBeInTheDocument();
    openDropdown();
    expect(screen.getAllByRole("menuitemradio").map((i) => i.textContent)).toEqual([
      "Not shown (team.example.com)",
      "Acme Engineering (omni.example)",
      "https://plain.example/",
    ]);
    const heading = screen.getByText("Recent servers");
    expect(heading).toHaveClass("text-muted-foreground");
    expect(heading.nextElementSibling).toHaveAccessibleName("Acme Engineering (omni.example)");
    expect(screen.getByText("(omni.example)")).toHaveClass("text-muted-foreground");
    expect(screen.getByText("https://plain.example/")).not.toHaveClass("text-muted-foreground");
  });

  it("lists every server and selects without connecting until the main button is clicked", () => {
    const props = renderLanding({
      managedServers: ["https://team.example.com", "https://other.example.com/"],
      managedServerNames: { "https://other.example.com/": "Engineering" },
      recentServers: ["https://old.example.com/"],
    });
    openDropdown();
    expect(screen.getAllByRole("menuitemradio").map((i) => i.textContent)).toEqual([
      "team (team.example.com)",
      "Engineering (other.example.com)",
      "https://old.example.com/",
    ]);
    expect(screen.getByRole("menuitemradio", { name: "team (team.example.com)" })).toBeChecked();
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Engineering (other.example.com)" }));
    expect(props.onJoinManaged).not.toHaveBeenCalled();
    expect(props.onJoinUrl).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    const join = screen.getByRole("button", { name: "Join your team (Engineering)" });

    openDropdown();
    const selected = screen.getByRole("menuitemradio", {
      name: "Engineering (other.example.com)",
    });
    expect(selected).toBeChecked();
    expect(selected).toHaveAttribute("data-active", "true");
    for (const item of screen.getAllByRole("menuitemradio")) {
      expect(item.querySelector("svg.lucide-users")).toBeInTheDocument();
    }
    expect(
      screen.getByRole("menuitemradio", { name: "team (team.example.com)" }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("menuitemradio", { name: "team (team.example.com)" }),
    ).not.toHaveAttribute("data-active");
    fireEvent.click(selected);
    fireEvent.click(join);
    expect(props.onJoinManaged).toHaveBeenCalledExactlyOnceWith("https://other.example.com/");

    openDropdown();
    fireEvent.click(screen.getByRole("menuitemradio", { name: "https://old.example.com/" }));
    expect(props.onJoinUrl).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Join your team (old.example.com)" }));
    expect(props.onJoinUrl).toHaveBeenCalledExactlyOnceWith("https://old.example.com/");

    openDropdown();
    fireEvent.click(screen.getByRole("menuitemradio", { name: "team (team.example.com)" }));
    fireEvent.click(screen.getByRole("button", { name: "Join your team (team)" }));
    expect(props.onJoinManaged).toHaveBeenLastCalledWith("https://team.example.com");
  });

  it("selects a typed URL before joining, and rejects an invalid one", () => {
    const props = renderLanding({ managedServers: ["https://team.example.com"] });
    openDropdown();
    const input = screen.getByLabelText("Server URL");

    fireEvent.change(input, { target: { value: "ftp://nope" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByRole("alert")).toHaveTextContent(/valid http\(s\) server url/i);
    expect(props.onJoinUrl).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "https://typed.example.com/x" } });
    fireEvent.click(screen.getByRole("button", { name: "Select server" }));
    expect(props.onJoinUrl).not.toHaveBeenCalled();
    openDropdown();
    const selected = screen.getByRole("menuitemradio", { name: "https://typed.example.com/" });
    expect(selected).toBeChecked();
    fireEvent.click(selected);
    fireEvent.click(screen.getByRole("button", { name: "Join your team (typed.example.com)" }));
    expect(props.onJoinUrl).toHaveBeenCalledExactlyOnceWith("https://typed.example.com/");
  });

  it("shows a connect error above the preset CTA", () => {
    renderLanding({ managedServers: ["https://team.example.com"], error: "boom" });
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
  });
});
