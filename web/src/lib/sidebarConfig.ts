import { createContext } from "react";
import { appConfig } from "@/appConfig";

export type { SidebarConfig } from "@/appConfig";

export const sidebarConfig = appConfig.sidebar;
export const SidebarConfigContext = createContext(sidebarConfig);
export const PinCapacityContext = createContext(false);
