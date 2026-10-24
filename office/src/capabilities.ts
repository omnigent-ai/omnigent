// Connected-instance capabilities, loaded once and shared through context.

import { createContext, useContext } from "react";
import type { RuntimeOptions } from "./api.ts";

export const RuntimeOptionsContext = createContext<RuntimeOptions | null>(null);

export function useRuntimeOptions(): RuntimeOptions | null {
  return useContext(RuntimeOptionsContext);
}
