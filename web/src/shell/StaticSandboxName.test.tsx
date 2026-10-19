import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { StaticSandboxName } from "./StaticSandboxName";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("renames, reports failures without closing, and clears the optional label", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Try again" }), { status: 503 }))
    .mockResolvedValue(new Response(JSON.stringify({ host_id: "box" })));
  render(
    <QueryClientProvider client={client}>
      <StaticSandboxName
        host={{
          host_id: "box",
          name: "controller-name",
          display_name: "Old label",
          owner: "alice",
          status: "offline",
          sandbox_attached: true,
        }}
      />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Edit static sandbox name" }));
  const input = screen.getByRole("textbox", { name: "Sandbox name" });
  expect(input).toHaveValue("Old label");
  fireEvent.change(input, { target: { value: " My project " } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Try again");
  expect(invalidate).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(fetch).toHaveBeenLastCalledWith(
    "/v1/hosts/box",
    expect.objectContaining({
      method: "PATCH",
      body: JSON.stringify({ display_name: "My project" }),
    }),
  );
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["hosts"] });
  fireEvent.click(screen.getByRole("button", { name: "Edit static sandbox name" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Sandbox name" }), {
    target: { value: "" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(fetch).toHaveBeenLastCalledWith(
    "/v1/hosts/box",
    expect.objectContaining({ method: "PATCH", body: JSON.stringify({ display_name: null }) }),
  );
});
