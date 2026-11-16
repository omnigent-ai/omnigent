import { createContext } from "react";
import type { CellRunAction } from "@/components/ai-elements/CodeCellRun";

/** Runs chat Python blocks in the open session; `null` hides the Run action. */
export const CellRunContext = createContext<CellRunAction | null>(null);
