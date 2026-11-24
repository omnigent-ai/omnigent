import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PendingAttachment } from "./PendingAttachment";
import { uploadProgressKey, useUploadProgressStore } from "@/store/uploadProgress";
import type { UploadFailure } from "@/store/types";

const messageId = "pend_1";
const fileId = "pending:big.zip";
const key = uploadProgressKey(messageId, fileId);

function renderAttachment(failure?: UploadFailure) {
  return render(
    <PendingAttachment
      messageId={messageId}
      fileId={fileId}
      name="big.zip"
      failure={failure}
      fallback={<span>plain chip</span>}
    />,
  );
}

beforeEach(() => {
  useUploadProgressStore.setState({ uploads: {} });
});

describe("PendingAttachment", () => {
  it("renders the fallback when nothing is uploading or failed", () => {
    renderAttachment();
    expect(screen.getByText("plain chip")).toBeInTheDocument();
  });

  it("shows a determinate bar, how much has gone, and Cancel while it uploads", () => {
    const cancel = vi.fn();
    useUploadProgressStore.setState({
      uploads: {
        [key]: { name: "big.zip", size: 2048, loaded: 512, total: 2048, cancel },
      },
    });
    renderAttachment();

    const bar = screen.getByRole("progressbar", { name: "Uploading big.zip" });
    expect(bar).toHaveAttribute("aria-valuetext", "25%");
    expect(screen.getByText("512 B of 2 KB")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel upload of big.zip" }));
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("shows an indeterminate bar with the file's size when progress is unknown", () => {
    useUploadProgressStore.setState({
      uploads: {
        [key]: { name: "big.zip", size: 2048, loaded: 0, total: null, cancel: vi.fn() },
      },
    });
    renderAttachment();

    const bar = screen.getByRole("progressbar", { name: "Uploading big.zip" });
    expect(bar).toHaveAttribute("aria-valuetext", "Uploading");
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel upload of big.zip" })).toBeInTheDocument();
  });

  it("labels a zero-byte total by the file's size, matching its indeterminate bar", () => {
    useUploadProgressStore.setState({
      uploads: { [key]: { name: "big.zip", size: 2048, loaded: 0, total: 0, cancel: vi.fn() } },
    });
    renderAttachment();
    expect(screen.getByRole("progressbar", { name: "Uploading big.zip" })).toHaveAttribute(
      "aria-valuetext",
      "Uploading",
    );
    expect(screen.getByText("2 KB")).toBeInTheDocument();
  });

  it("never shows more loaded than the total, so the label agrees with the bar", () => {
    useUploadProgressStore.setState({
      uploads: {
        [key]: { name: "big.zip", size: 2048, loaded: 3072, total: 2048, cancel: vi.fn() },
      },
    });
    renderAttachment();
    expect(screen.getByRole("progressbar", { name: "Uploading big.zip" })).toHaveAttribute(
      "aria-valuetext",
      "100%",
    );
    expect(screen.getByText("2 KB of 2 KB")).toBeInTheDocument();
  });

  it("omits Cancel once the owner no longer allows it", () => {
    useUploadProgressStore.setState({
      uploads: { [key]: { name: "big.zip", size: 10, loaded: 0, total: 10 } },
    });
    renderAttachment();
    expect(screen.queryByRole("button", { name: "Cancel upload of big.zip" })).toBeNull();
  });

  it("shows a failed upload's reason with Retry and Remove", () => {
    const retry = vi.fn();
    const remove = vi.fn();
    renderAttachment({
      fileId,
      message: "Attachment quota reached",
      stableId: "stable",
      retry,
      remove,
    });

    expect(screen.getByText("Attachment quota reached")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry upload of big.zip" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove failed upload of big.zip" }));
    expect(retry).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledTimes(1);
  });

  it("leaves other attachments of a failed message on their fallback", () => {
    renderAttachment({
      fileId: "pending:other.zip",
      message: "Too big",
      stableId: "stable",
      retry: vi.fn(),
      remove: vi.fn(),
    });
    expect(screen.getByText("plain chip")).toBeInTheDocument();
  });
});
