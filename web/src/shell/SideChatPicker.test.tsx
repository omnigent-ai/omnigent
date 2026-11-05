import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { setSessionParent } from "@/lib/sessionHost";
import { SideChatPicker } from "./SideChatPicker";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/sessionHost", () => ({ setSessionParent: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPicker() {
  const onOpen = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <SideChatPicker conversationId="parent" onOpen={onOpen} />
    </QueryClientProvider>,
  );
  return onOpen;
}

it("fetches 20 at a time on demand and opens only the selected side chat", async () => {
  let finishPage!: (response: Response) => void;
  const nextPage = new Promise<Response>((resolve) => {
    finishPage = resolve;
  });
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          data: [{ id: "one", title: "First question", created_at: 100 }],
          has_more: true,
          last_id: "one",
        }),
      ),
    )
    .mockReturnValueOnce(nextPage);
  const onOpen = renderPicker();
  expect(authenticatedFetch).not.toHaveBeenCalled();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Browse side chats" }));
  await screen.findByText("First question");
  expect(authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/parent/side_chats?limit=20");
  expect(onOpen).not.toHaveBeenCalled();
  await user.click(screen.getByRole("menuitem", { name: "Next 20" }));
  expect(screen.getByText("First question")).toBeInTheDocument();
  expect(screen.getByRole("menuitem", { name: "Next 20" })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  finishPage(
    new Response(
      JSON.stringify({
        data: [{ id: "two", title: "Second question", created_at: 200 }],
        has_more: false,
        last_id: "two",
      }),
    ),
  );
  await user.click(await screen.findByText("Second question"));
  expect(authenticatedFetch).toHaveBeenLastCalledWith(
    "/v1/sessions/parent/side_chats?limit=20&after=one",
  );
  expect(setSessionParent).toHaveBeenCalledWith("two", "parent");
  expect(onOpen).toHaveBeenCalledExactlyOnceWith("two");
  await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
});

it.each([404, 500])("handles a %s response without changing local tabs", async (status) => {
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response("{}", { status }));
  const onOpen = renderPicker();
  await userEvent.setup().click(screen.getByRole("button", { name: "Browse side chats" }));
  await screen.findByText(status === 404 ? "No saved side chats" : "Could not load side chats");
  expect(onOpen).not.toHaveBeenCalled();
});
