import { useState } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TicketImageAttachButton, useTicketImageUpload } from "./ticketImages";

const uploadTicketImage = vi.hoisted(() => vi.fn());
vi.mock("@/lib/projectTicketsApi", () => ({ uploadTicketImage }));

const url = `/v1/projects/${"a".repeat(32)}/ticket-attachments/${"b".repeat(32)}/content`;

function Harness() {
  const [text, setText] = useState("Steps");
  const images = useTicketImageUpload(async () => "project-1", setText);
  return (
    <>
      <TicketImageAttachButton onFiles={images.attach} />
      <output data-testid="text">{text}</output>
    </>
  );
}

afterEach(() => {
  cleanup();
  uploadTicketImage.mockReset();
});

describe("TicketImageAttachButton", () => {
  it("uploads picked images and appends their markdown, skipping other files", async () => {
    uploadTicketImage.mockResolvedValue({ filename: "shot.png", url });
    const { container } = render(<Harness />);
    const input = container.querySelector<HTMLInputElement>("input[type=file]");
    expect(input).not.toBeNull();
    const user = userEvent.setup({ applyAccept: false });
    await user.upload(input!, [
      new File(["png"], "shot.png", { type: "image/png" }),
      new File(["txt"], "notes.txt", { type: "text/plain" }),
    ]);
    await waitFor(() =>
      expect(screen.getByTestId("text").textContent).toBe(`Steps\n![shot.png](${url})\n`),
    );
    expect(uploadTicketImage).toHaveBeenCalledTimes(1);
    expect(uploadTicketImage.mock.calls[0][0]).toBe("project-1");
    expect(screen.getByRole("button", { name: /attach image/i })).toBeTruthy();
  });
});
