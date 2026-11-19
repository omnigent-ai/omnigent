import { afterEach, expect, it, vi } from "vitest";
import { setTicketSessionInvolvement } from "./projectTicketsApi";

afterEach(() => vi.unstubAllGlobals());

it.each(["Planning the approach", ""])(
  "sends involvement, including an explicit clear (%s)",
  async (involvement) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "ticket" })));
    vi.stubGlobal("fetch", fetchMock);
    expect(
      await setTicketSessionInvolvement("project one", "ticket", "worker", involvement),
    ).toEqual({ id: "ticket" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/project%20one/tickets/ticket/sessions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      session_id: "worker",
      action: "attach",
      involvement,
    });
  },
);
