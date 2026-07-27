import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LandingAgentMascot, landingAgentMascotVariant } from "./LandingAgentMascot";

describe("LandingAgentMascot", () => {
  it("uses the default Otto treatment for other agents", () => {
    render(<LandingAgentMascot agentName="claude-native-ui" />);

    const mascot = screen.getByTestId("new-chat-landing-mascot");
    expect(mascot.dataset.variant).toBe("otto");
    expect(mascot.className).toContain("h-[88px]");
    expect(mascot.className).toContain("w-[108px]");
    expect(mascot.className).toContain("justify-center");
    expect(screen.queryByTestId("willy-paintbrush")).toBeNull();
  });

  it("adds the studio treatment for Willy", () => {
    render(<LandingAgentMascot agentName="WILLY" />);

    expect(screen.getByTestId("new-chat-landing-mascot").dataset.variant).toBe("willy");
    expect(screen.getByTestId("willy-paintbrush")).toBeTruthy();
    expect(screen.getByTestId("otto-painted-body")).toBeTruthy();
    expect(screen.getByLabelText("Willy")).toBeTruthy();
  });

  it("resolves only Willy to the specialized variant", () => {
    expect(landingAgentMascotVariant(" willy ")).toBe("willy");
    expect(landingAgentMascotVariant("polly")).toBe("otto");
    expect(landingAgentMascotVariant(undefined)).toBe("otto");
  });
});
