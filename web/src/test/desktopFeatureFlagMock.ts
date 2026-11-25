// A stand-in for `@/hooks/useDesktopFeatureFlag` that implements every hook it exports from one
// read function, so a test that mocks the module keeps working when a hook is added to it:
//
//   vi.mock("@/hooks/useDesktopFeatureFlag", async () =>
//     (await import("@/test/desktopFeatureFlagMock")).desktopFeatureFlagModule((name, fallback) => ({
//       value: flags.get(name) ?? fallback,
//       resolved: true,
//     })),
//   );

import type * as DesktopFeatureFlagHooks from "@/hooks/useDesktopFeatureFlag";
import type { DesktopFeatureFlagReading, DesktopFeatureFlagState } from "@/lib/desktopFeatureFlags";
import type { DesktopFeatureFlagMode } from "@/lib/nativeBridge";

type FlagValue = boolean | number | null;

/** What the test's shell answers for a flag; it may call hooks, as the real ones do. */
export type DesktopFeatureFlagRead = (
  name: string,
  fallback: FlagValue,
  mode: DesktopFeatureFlagMode,
) => DesktopFeatureFlagState<FlagValue>;

/**
 * The module's hooks, all answered by `read`. Typed as the real module, so a hook it adds
 * without one here fails type-check.
 */
export function desktopFeatureFlagModule(
  read: DesktopFeatureFlagRead,
): typeof DesktopFeatureFlagHooks {
  return {
    useDesktopFeatureFlag: ((name: string, fallback: FlagValue, mode = "value") =>
      read(name, fallback, mode)) as typeof DesktopFeatureFlagHooks.useDesktopFeatureFlag,
    useDesktopFeatureFlagReading: ((
      name: string,
      fallback: FlagValue,
      mode: DesktopFeatureFlagMode = "value",
      enabled = true,
    ): DesktopFeatureFlagReading<FlagValue> => {
      // A disabled reader reads nothing, as the real one does.
      if (!enabled) return { value: fallback, resolved: false, source: "pending" };
      const state = read(name, fallback, mode);
      return { ...state, source: state.resolved ? "shell" : "pending" };
    }) as typeof DesktopFeatureFlagHooks.useDesktopFeatureFlagReading,
    // Remembers nothing between tests: the test's shell answers at once.
    useRememberedDesktopFeatureFlag: (name: string, fallback: boolean) =>
      read(name, fallback, "value").value === true,
  };
}
