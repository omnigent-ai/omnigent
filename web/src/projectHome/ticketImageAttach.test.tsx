import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { TicketImageAttachButton, TicketImagePreviews, useTicketImageUpload } from "./ticketImages";

const uploadTicketImage = vi.hoisted(() => vi.fn());
vi.mock("@/lib/projectTicketsApi", () => ({ uploadTicketImage }));

const url = (n: string) =>
  `/v1/projects/${"a".repeat(32)}/ticket-attachments/${n.repeat(32)}/content`;

function Harness() {
  const images = useTicketImageUpload(async () => "project-1");
  return (
    <>
      <textarea aria-label="Text" defaultValue="Steps" onPaste={images.onPaste} />
      <TicketImageAttachButton onFiles={images.attach} />
      <TicketImagePreviews upload={images} />
      <output data-testid="body">{images.withImages("Steps")}</output>
      <output data-testid="uploading">{String(images.uploading)}</output>
    </>
  );
}

beforeAll(() => {
  URL.createObjectURL ??= () => "blob:preview";
  URL.revokeObjectURL ??= () => {};
});

afterEach(() => {
  cleanup();
  uploadTicketImage.mockReset();
});

describe("ticket image attachments", () => {
  it("shows picked images as removable thumbnails and appends their markdown on send", async () => {
    uploadTicketImage
      .mockResolvedValueOnce({ filename: "one.png", url: url("b") })
      .mockResolvedValueOnce({ filename: "two.png", url: url("c") });
    const { container } = render(<Harness />);
    const input = container.querySelector<HTMLInputElement>("input[type=file]")!;
    const user = userEvent.setup({ applyAccept: false });
    await user.upload(input, [
      new File(["png"], "one.png", { type: "image/png" }),
      new File(["txt"], "notes.txt", { type: "text/plain" }),
      new File(["png"], "two.png", { type: "image/png" }),
    ]);
    await waitFor(() => expect(screen.getByTestId("uploading").textContent).toBe("false"));
    // Thumbnails, not markdown, while composing; other file types are skipped.
    expect(screen.getByRole("img", { name: "one.png" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "two.png" })).toBeTruthy();
    expect((screen.getByLabelText("Text") as HTMLTextAreaElement).value).toBe("Steps");
    expect(uploadTicketImage).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("body").textContent).toBe(
      `Steps\n\n![one.png](${url("b")})\n![two.png](${url("c")})`,
    );
    await user.click(screen.getByRole("button", { name: /remove one\.png/i }));
    expect(screen.queryByRole("img", { name: "one.png" })).toBeNull();
    expect(screen.getByTestId("body").textContent).toBe(`Steps\n\n![two.png](${url("c")})`);
  });

  it("accepts videos and appends a video link", async () => {
    uploadTicketImage.mockResolvedValue({
      filename: "clip.webm",
      url: url("f"),
      content_type: "video/webm",
    });
    const { container } = render(<Harness />);
    const user = userEvent.setup({ applyAccept: false });
    await user.upload(container.querySelector<HTMLInputElement>("input[type=file]")!, [
      new File(["webm"], "clip.webm", { type: "video/webm" }),
    ]);
    await waitFor(() =>
      expect(screen.getByTestId("body").textContent).toBe(
        `Steps\n\n![clip.webm](${url("f")}?type=video)`,
      ),
    );
    expect(screen.getByText("clip.webm")).toBeTruthy();
  });

  it("treats a pasted image like an attached one", async () => {
    uploadTicketImage.mockResolvedValue({ filename: "pasted.png", url: url("d") });
    render(<Harness />);
    const file = new File(["png"], "pasted.png", { type: "image/png" });
    fireEvent.paste(screen.getByLabelText("Text"), {
      clipboardData: {
        files: [file],
        items: [{ kind: "file", type: "image/png", getAsFile: () => file }],
      },
    });
    await waitFor(() =>
      expect(screen.getByTestId("body").textContent).toBe(`Steps\n\n![pasted.png](${url("d")})`),
    );
    expect(screen.getByRole("img", { name: "pasted.png" })).toBeTruthy();
    expect((screen.getByLabelText("Text") as HTMLTextAreaElement).value).toBe("Steps");
  });

  it("keeps uploading the rest, in order, after one image fails", async () => {
    uploadTicketImage
      .mockRejectedValueOnce(new Error("Too large"))
      .mockResolvedValueOnce({ filename: "ok.png", url: url("e") });
    const { container } = render(<Harness />);
    const user = userEvent.setup({ applyAccept: false });
    await user.upload(container.querySelector<HTMLInputElement>("input[type=file]")!, [
      new File(["png"], "big.png", { type: "image/png" }),
      new File(["png"], "ok.png", { type: "image/png" }),
    ]);
    await waitFor(() =>
      expect(screen.getByTestId("body").textContent).toBe(`Steps\n\n![ok.png](${url("e")})`),
    );
    expect(screen.queryByRole("img", { name: "big.png" })).toBeNull();
    expect(screen.getByRole("img", { name: "ok.png" })).toBeTruthy();
  });

  it("drops an image whose upload fails and reports the error", async () => {
    uploadTicketImage.mockRejectedValue(new Error("Too large"));
    const { container } = render(<Harness />);
    const user = userEvent.setup({ applyAccept: false });
    await user.upload(container.querySelector<HTMLInputElement>("input[type=file]")!, [
      new File(["png"], "big.png", { type: "image/png" }),
    ]);
    await waitFor(() => expect(screen.queryByRole("img", { name: "big.png" })).toBeNull());
    expect(screen.getByTestId("body").textContent).toBe("Steps");
  });
});
