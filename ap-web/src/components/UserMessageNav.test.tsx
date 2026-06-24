// Invariants:
// - hidden=true → returns null.
// - canPrev/canNext drive the `disabled` attribute (asserted explicitly
//   to catch a regression to aria-disabled, which wouldn't block clicks).

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { UserMessageNav } from "./UserMessageNav";
import i18n from "@/i18n";

const t = i18n.getFixedT(null, "nav");

function renderNav(props: Partial<React.ComponentProps<typeof UserMessageNav>>) {
  const merged = {
    goPrev: vi.fn(),
    goNext: vi.fn(),
    canPrev: true,
    canNext: true,
    hidden: false,
    ...props,
  };
  render(
    <TooltipProvider>
      <UserMessageNav {...merged} />
    </TooltipProvider>,
  );
  return merged;
}

afterEach(cleanup);

describe("UserMessageNav", () => {
  it("renders nothing when hidden", () => {
    renderNav({ hidden: true });
    expect(screen.queryByLabelText(t("previousUserMessage"))).toBeNull();
    expect(screen.queryByLabelText(t("nextUserMessage"))).toBeNull();
  });

  it("renders both buttons when there is content to navigate", () => {
    renderNav({});
    expect(screen.getByLabelText(t("previousUserMessage"))).toBeEnabled();
    expect(screen.getByLabelText(t("nextUserMessage"))).toBeEnabled();
  });

  it("disables Previous when canPrev=false", () => {
    const props = renderNav({ canPrev: false });
    const btn = screen.getByLabelText(t("previousUserMessage"));
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(props.goPrev).not.toHaveBeenCalled();
  });

  it("disables Next when canNext=false", () => {
    const props = renderNav({ canNext: false });
    const btn = screen.getByLabelText(t("nextUserMessage"));
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(props.goNext).not.toHaveBeenCalled();
  });

  it("invokes goPrev / goNext on click", () => {
    const props = renderNav({});
    fireEvent.click(screen.getByLabelText(t("previousUserMessage")));
    fireEvent.click(screen.getByLabelText(t("nextUserMessage")));
    expect(props.goPrev).toHaveBeenCalledOnce();
    expect(props.goNext).toHaveBeenCalledOnce();
  });
});
