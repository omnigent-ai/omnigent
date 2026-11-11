import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ScheduledTaskShare } from "@/lib/scheduledTasksApi";
import { AutomationShareFields, MAX_AUTOMATION_SHARES, sameShares } from "./AutomationShareFields";

afterEach(cleanup);

function renderFields(
  initial: ScheduledTaskShare[],
  {
    info = {},
    ownerId = "alice@example.com",
  }: { info?: Partial<ServerInfo>; ownerId?: string } = {},
) {
  const onValue = vi.fn();
  function Harness() {
    const [value, setValue] = useState(initial);
    return (
      <AutomationShareFields
        idPrefix="share"
        value={value}
        onChange={(next) => {
          onValue(next);
          setValue(next);
        }}
        ownerId={ownerId}
      />
    );
  }
  render(
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, scheduled_task_sharing: true, ...info }}>
      <Harness />
    </CapabilitiesProvider>,
  );
  return onValue;
}

function add(userId: string) {
  fireEvent.change(screen.getByLabelText("User or group"), { target: { value: userId } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
}

describe("AutomationShareFields", () => {
  it("adds a share at read level and removes it", () => {
    const onValue = renderFields([]);
    add("team.engineering");
    expect(onValue).toHaveBeenLastCalledWith([{ userId: "team.engineering", level: 1 }]);
    expect(screen.getByText("team.engineering")).toBeTruthy();
    expect(screen.getByLabelText("User or group")).toHaveProperty("value", "");

    fireEvent.click(
      screen.getByRole("button", { name: "Stop sharing runs with team.engineering" }),
    );
    expect(onValue).toHaveBeenLastCalledWith([]);
  });

  it("refuses the owner and a grantee already listed", () => {
    const onValue = renderFields([{ userId: "team.engineering", level: 1 }]);
    add("alice@example.com");
    expect(screen.getByRole("alert").textContent).toBe("You already own every run.");
    add("team.engineering");
    expect(screen.getByRole("alert").textContent).toBe(
      "Runs are already shared with team.engineering.",
    );
    expect(onValue).not.toHaveBeenCalled();
  });

  it("explains edit access only when someone has it", () => {
    renderFields([{ userId: "bob@example.com", level: 2 }]);
    expect(screen.getByTestId("share-edit-note")).toBeTruthy();
    cleanup();
    renderFields([{ userId: "bob@example.com", level: 1 }]);
    expect(screen.queryByTestId("share-edit-note")).toBeNull();
  });

  it.each([
    ["sharing is off", { sharing_mode: "off" }],
    ["the server cannot share runs", { scheduled_task_sharing: false }],
  ] as const)("offers only to stop sharing when %s", (_, info) => {
    const onValue = renderFields([{ userId: "team.engineering", level: 1 }], { info });
    expect(screen.queryByLabelText("User or group")).toBeNull();
    expect(screen.queryByLabelText("Permission for team.engineering")).toBeNull();
    expect(screen.getByText("team.engineering")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Stop sharing runs" }));
    expect(onValue).toHaveBeenLastCalledWith([]);
    expect(screen.getByText("Runs won't be shared after you save.")).toBeTruthy();
  });

  it("changes a recipient's level, and adds only at Read on a read-only server", () => {
    const onValue = renderFields([{ userId: "team.engineering", level: 1 }]);
    fireEvent.click(screen.getByRole("combobox", { name: "Permission for team.engineering" }));
    fireEvent.click(screen.getByRole("option", { name: "Edit" }));
    expect(onValue).toHaveBeenLastCalledWith([{ userId: "team.engineering", level: 2 }]);
    cleanup();

    const readOnly = renderFields([], { info: { sharing_mode: "read_only" } });
    fireEvent.click(screen.getByRole("combobox", { name: "Permission for new share" }));
    expect(screen.queryByRole("option", { name: "Edit" })).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: "Read" }));
    add("bob@example.com");
    expect(readOnly).toHaveBeenLastCalledWith([{ userId: "bob@example.com", level: 1 }]);
  });

  it("stops adding at the server's limit", () => {
    const onValue = renderFields(
      Array.from({ length: MAX_AUTOMATION_SHARES - 1 }, (_, index) => ({
        userId: `user${index}@example.com`,
        level: 1,
      })),
    );
    add("last@example.com");
    expect(onValue.mock.lastCall?.[0]).toHaveLength(MAX_AUTOMATION_SHARES);
    expect(screen.getByLabelText("User or group")).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Add" })).toHaveProperty("disabled", true);
    expect(screen.getByText(/up to 10 people or groups/)).toBeTruthy();
  });
});

describe("sameShares", () => {
  it("ignores order but not levels", () => {
    const team = { userId: "team.engineering", level: 1 };
    const bob = { userId: "bob@example.com", level: 2 };
    expect(sameShares([team, bob], [bob, team])).toBe(true);
    expect(sameShares([team], [{ ...team, level: 2 }])).toBe(false);
    expect(sameShares([], [team])).toBe(false);
  });
});
