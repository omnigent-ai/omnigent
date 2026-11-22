import { createContext, type ReactNode } from "react";
import type { Bubble } from "@/lib/renderItems";

export interface ChatPresentation {
  header?: (empty: boolean) => ReactNode;
  assistant?: { name: string; icon?: ReactNode };
  /** Supplements are keyed by the existing bubble's stable ID; grouping stays unchanged. */
  responseExtras?: (bubbles: Bubble[]) => ReadonlyMap<string, ReactNode>;
}

export const AssistantPresentationContext = createContext<{
  identity?: ChatPresentation["assistant"];
  extras?: ReadonlyMap<string, ReactNode>;
}>({});
