import { onTestFinished, vi } from "vitest";

import { MOBILE_QUERY } from "@/hooks/useIsMobileViewport";

/** Report a phone-sized viewport to `useIsMobileViewport` until the test ends. */
export function mockMobileViewport() {
  const mediaQuery = window.matchMedia(MOBILE_QUERY);
  const matchMedia = vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    ...mediaQuery,
    media: query,
    matches: query === MOBILE_QUERY,
  }));
  onTestFinished(() => matchMedia.mockRestore());
}
