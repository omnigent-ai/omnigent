import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { DEFAULT_DATA_CONTROLS, FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FileDownloadButton } from "./FileDownloadButton";

afterEach(cleanup);

function renderButton(allowDownloads: boolean) {
  return render(
    <CapabilitiesProvider
      info={{
        ...FALLBACK_SERVER_INFO,
        data_controls: { ...DEFAULT_DATA_CONTROLS, allowDownloads },
      }}
    >
      <TooltipProvider>
        <FileDownloadButton conversationId="conv_1" path="src/main.py" />
      </TooltipProvider>
    </CapabilitiesProvider>,
  );
}

describe("FileDownloadButton", () => {
  it("offers the download by default", () => {
    renderButton(true);
    expect(screen.getByRole("button", { name: "Download main.py" })).toBeInTheDocument();
  });

  it("keeps an empty slot of the same size when the server turns downloads off", () => {
    const { container } = renderButton(false);
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.querySelector('span[aria-hidden="true"]')).toHaveClass("size-[18px]");
  });
});
