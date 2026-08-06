import { useMediaQuery } from "./useMediaQuery";

// `any-pointer` (not `pointer`): a touchscreen laptop's PRIMARY pointer is the
// trackpad, but its touch digitizer should still enable touch-only affordances.
const COARSE_POINTER_QUERY = "(any-pointer: coarse)";

/**
 * True when the device has at least one coarse (touch) pointer. Reactive and
 * SSR-safe (`false` on the server).
 */
export function useCoarsePointer(): boolean {
  return useMediaQuery(COARSE_POINTER_QUERY);
}
