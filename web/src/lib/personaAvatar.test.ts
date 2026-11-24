import { describe, expect, it } from "vitest";
import { personaColor, personaInitials } from "./personaAvatar";
import { sidebarPersonaName } from "@/projectHome/usePersonaSessions";

describe("persona avatars", () => {
  it("takes one or two initials from the name", () => {
    expect(personaInitials("PM")).toBe("PM");
    expect(personaInitials("QA")).toBe("QA");
    expect(personaInitials("Designer")).toBe("D");
    expect(personaInitials("Orchestrator")).toBe("O");
    expect(personaInitials("release manager")).toBe("RM");
    expect(personaInitials("  ")).toBe("?");
  });

  it("colors by name, the same everywhere and regardless of case", () => {
    expect(personaColor("PM")).toBe(personaColor("pm"));
    expect(personaColor("PM")).toMatch(/^#[0-9a-f]{6}$/);
    const colors = new Set(["PM", "Designer", "Orchestrator", "QA", "Writer"].map(personaColor));
    expect(colors.size).toBeGreaterThan(1);
  });

  it("marks only Orchestrator and identity sessions as personas in the sidebar", () => {
    const orchestrators = new Set(["main"]);
    expect(sidebarPersonaName({ id: "main", title: "Orchestrator" }, orchestrators)).toBe(
      "Lead",
    );
    const persona = { id: "s1", title: "PM", labels: { "omnigent.project_identity_id": "pm" } };
    expect(sidebarPersonaName(persona, orchestrators)).toBe("PM");
    expect(sidebarPersonaName({ id: "w1", title: "Worker", labels: {} }, orchestrators)).toBeNull();
  });
});
