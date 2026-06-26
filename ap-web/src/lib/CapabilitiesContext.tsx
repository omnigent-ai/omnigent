/**
 * React context holding the result of the ``/v1/info`` probe.
 *
 * Populated by :class:`CapabilitiesProvider` in ``main.tsx`` after
 * the probe resolves, then read by ``App.tsx`` (to gate route
 * registration) and ``AccountMenu`` (to gate rendering). Components
 * use the typed :func:`useServerInfo` hook to consume the value;
 * the raw ``Context`` is exported for testing convenience.
 *
 * The provider's default value is ``"loading"`` so consumers can
 * distinguish "probe in flight, do nothing yet" from "probe done,
 * accounts is off". This matters for the AccountMenu which would
 * flash an empty placeholder on first paint otherwise.
 */

import { createContext, type ReactNode, useContext } from "react";
import type { ServerInfo } from "./capabilities";

type CapabilitiesValue = ServerInfo | "loading";

export const CapabilitiesContext = createContext<CapabilitiesValue>("loading");

export function CapabilitiesProvider({
  info,
  children,
}: {
  // Accepts the ``"loading"`` sentinel so main.tsx can mount the tree before
  // the boot probe resolves (the app-shell skeleton renders during that
  // window); flips to the resolved ``ServerInfo`` once the probe settles.
  info: CapabilitiesValue;
  children: ReactNode;
}) {
  return <CapabilitiesContext.Provider value={info}>{children}</CapabilitiesContext.Provider>;
}

/**
 * Read the current server-info value.
 *
 * Returns ``"loading"`` while the boot probe is still in flight.
 * Once resolved, returns the :class:`ServerInfo` shape — never
 * null, since the probe's failure path resolves to the OFF
 * sentinel instead of erroring out.
 */
export function useServerInfo(): CapabilitiesValue {
  return useContext(CapabilitiesContext);
}
