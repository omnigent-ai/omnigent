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
  const onClose = vi.fn();
  const dialog = (key: string) => (
    <QueryClientProvider client={client}>
      <StaticSandboxName
        key={key}
        onClose={onClose}
        host={{
          host_id: "box",
          name: "controller-name",
          display_name: "Old label",
          owner: "alice",
          status: "offline",
          sandbox_attached: true,
        }}
      />
    </QueryClientProvider>
  );
  const { rerender } = render(dialog("rename"));
  const input = screen.getByRole("textbox", { name: "Sandbox name" });
  expect(input).toHaveValue("Old label");
  fireEvent.change(input, { target: { value: " My project " } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Try again");
  expect(invalidate).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  expect(fetch).toHaveBeenLastCalledWith(
    "/v1/hosts/box",
    expect.objectContaining({
      method: "PATCH",
      body: JSON.stringify({ display_name: "My project" }),
    }),
  );
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["hosts"] });
  rerender(dialog("clear"));
  fireEvent.change(screen.getByRole("textbox", { name: "Sandbox name" }), {
    target: { value: "" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(2));
  expect(fetch).toHaveBeenLastCalledWith(
    "/v1/hosts/box",
    expect.objectContaining({ method: "PATCH", body: JSON.stringify({ display_name: null }) }),
  );
});
