import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { listPermissions } from "@/lib/permissionsApi";
import meta from "./PermissionsModal.designs.stories";

const sessionId = "sharing-design-preview";

async function chooseOption(label: string, option: string) {
  const control = screen.getByRole("combobox", { name: label });
  await waitFor(() => expect(control).toBeEnabled());
  control.focus();
  fireEvent.keyDown(control, { key: "Enter" });
  fireEvent.click(await screen.findByRole("option", { name: option }));
  await waitFor(() => expect(control).toBeEnabled());
}

afterEach(cleanup);

describe("sharing design shared permissions", () => {
  it("shows the workspace helper only while file sharing is enabled", async () => {
    render(<meta.component design="access-first" scenario="Private" />);
    const toggle = await screen.findByRole("switch", {
      name: "Viewers can browse workspace files",
    });
    const helper = /People with Read access can browse files/;
    expect(screen.queryByText(helper)).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(await screen.findByText(helper)).toBeInTheDocument();
    expect(toggle).toBeChecked();
    await waitFor(() => expect(toggle).toBeEnabled());
    fireEvent.click(toggle);
    await waitFor(() => expect(screen.queryByText(helper)).not.toBeInTheDocument());
    expect(toggle).not.toBeChecked();
  });

  it.each([
    ["People added", "Only added people"],
    ["Link access enabled", "Anyone signed in"],
  ] as const)("shows both controls and text-only person roles for %s", async (scenario, access) => {
    render(<meta.component design="access-first" scenario={scenario} />);

    await screen.findByText("sam.taylor@example.com");
    expect(screen.getByRole("combobox", { name: "Who can access" })).toHaveTextContent(access);
    expect(screen.getByRole("combobox", { name: "What can they do?" })).toHaveTextContent("Read");
    expect(screen.getAllByRole("combobox")).toHaveLength(2);
    const accessRows = within(
      screen.getByRole("list", { name: "People with access" }),
    ).getAllByRole("listitem");
    expect(accessRows[0]).toHaveTextContent("you@example.com");
    expect(
      within(screen.getByRole("list", { name: "People with access" })).getAllByText("Read"),
    ).toHaveLength(access === "Anyone signed in" ? 3 : 2);
    if (access === "Anyone signed in") {
      expect(accessRows[1]).toHaveTextContent("Everyone");
      expect(screen.queryByRole("button", { name: "Add" })).not.toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: "User ID" })).not.toBeInTheDocument();
      const everyoneRow = screen.getByText("Everyone").closest("li");
      if (!everyoneRow) throw new Error("Everyone row not found");
      expect(everyoneRow).toHaveTextContent("Read");
      expect(within(everyoneRow).queryByRole("button")).not.toBeInTheDocument();
      expect(screen.queryAllByRole("button", { name: /^Remove access for / })).toHaveLength(0);
    } else {
      expect(screen.getByRole("button", { name: "Add" })).toBeInTheDocument();
      expect(screen.queryByText("Everyone")).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Remove access for sam.taylor@example.com" }),
      ).toBeEnabled();
    }
    expect(
      screen.queryByRole("button", { name: "Remove access for you@example.com" }),
    ).not.toBeInTheDocument();
  });

  it("updates every collaborator and gives new people the selected role", async () => {
    render(<meta.component design="access-first" scenario="People added" />);
    await screen.findByText("sam.taylor@example.com");

    await chooseOption("What can they do?", "Edit");
    expect(await listPermissions(sessionId)).toEqual(
      expect.arrayContaining([
        { user_id: "you@example.com", conversation_id: sessionId, level: 4 },
        { user_id: "alex.morgan@example.com", conversation_id: sessionId, level: 2 },
        { user_id: "sam.taylor@example.com", conversation_id: sessionId, level: 2 },
      ]),
    );
    expect(
      within(screen.getByRole("list", { name: "People with access" })).getAllByText("Edit"),
    ).toHaveLength(2);

    await chooseOption("Who can access", "Anyone signed in");
    expect(await listPermissions(sessionId)).toEqual(
      expect.arrayContaining([
        { user_id: "__public__", conversation_id: sessionId, level: 1 },
        { user_id: "alex.morgan@example.com", conversation_id: sessionId, level: 1 },
        { user_id: "sam.taylor@example.com", conversation_id: sessionId, level: 1 },
      ]),
    );
    expect(screen.queryAllByRole("button", { name: /^Remove access for / })).toHaveLength(0);

    await chooseOption("Who can access", "Only added people");
    await chooseOption("What can they do?", "Edit");
    expect(screen.queryByText("Everyone")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove access for sam.taylor@example.com" }),
    ).toBeEnabled();

    fireEvent.change(screen.getByRole("textbox", { name: "User ID" }), {
      target: { value: "new.person@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByText("new.person@example.com");
    expect(await listPermissions(sessionId)).toContainEqual({
      user_id: "new.person@example.com",
      conversation_id: sessionId,
      level: 2,
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Remove access for sam.taylor@example.com" }),
    );
    await waitFor(() =>
      expect(screen.queryByText("sam.taylor@example.com")).not.toBeInTheDocument(),
    );
    expect(
      (await listPermissions(sessionId)).map((permission) => permission.user_id),
    ).not.toContain("sam.taylor@example.com");
  });

  it("caps public access at Read and preserves the draft email when switching modes", async () => {
    render(<meta.component design="access-first" scenario="Private" />);
    await screen.findByText("you@example.com");

    await chooseOption("What can they do?", "Edit");
    expect(await listPermissions(sessionId)).toEqual([
      { user_id: "you@example.com", conversation_id: sessionId, level: 4 },
    ]);

    fireEvent.change(screen.getByRole("textbox", { name: "User ID" }), {
      target: { value: "draft.person@example.com" },
    });
    await chooseOption("Who can access", "Anyone signed in");
    expect(await listPermissions(sessionId)).toContainEqual({
      user_id: "__public__",
      conversation_id: sessionId,
      level: 1,
    });
    expect(screen.queryByRole("textbox", { name: "User ID" })).not.toBeInTheDocument();
    expect(screen.getByText("Everyone").closest("li")).toHaveTextContent("Read");
    expect(
      within(screen.getByRole("list", { name: "People with access" })).getAllByRole("listitem")[0],
    ).toHaveTextContent("you@example.com");
    await chooseOption("Who can access", "Only added people");
    expect(screen.queryByText("Everyone")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "User ID" })).toHaveValue(
      "draft.person@example.com",
    );
    expect(screen.getByRole("combobox", { name: "What can they do?" })).toHaveTextContent("Read");
  });
});
