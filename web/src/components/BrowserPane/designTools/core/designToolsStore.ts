// Each Browser tab's design-tool state: which tool is open, its drafts, and its phase.
// Drafts change only while `editing`, and every open and close starts a new `session`, so
// async work started in one session can't land in a tool opened since.
import { create } from "zustand";
import type { DesignModeElement } from "@/lib/designModePrompt";

export type Tool = "annotate" | "draw";

/** `preparing`: a send is being built (a screenshot taken), and the drafts are frozen. */
export type Phase = "editing" | "preparing";

/** A rect in the page's CSS pixels. */
export interface CssRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Hit {
  element: DesignModeElement;
  rect: CssRect;
  /** Where Annotate picked it, in the page's viewport. */
  at?: { x: number; y: number };
  /** The page's key for the element itself, to tell alike ones apart. */
  key?: string;
}

export type ShapeKind = "pen" | "rect" | "arrow" | "text";

export interface Shape {
  kind: ShapeKind;
  points: { x: number; y: number }[];
  color: string;
  /** The words, for a text mark. */
  text?: string;
}

export interface Drafts {
  /** Annotate: the picked element and the request. */
  picked: Hit | null;
  request: string;
  /** Draw: the marks and the note. */
  shapes: Shape[];
  note: string;
}

export interface TabTools extends Drafts {
  tool: Tool | null;
  phase: Phase;
  session: number;
}

const EMPTY_DRAFTS: Drafts = { picked: null, request: "", shapes: [], note: "" };

const CLOSED: TabTools = { ...EMPTY_DRAFTS, tool: null, phase: "editing", session: 0 };

export const useDesignTools = create<{ tabs: Record<string, TabTools> }>(() => ({ tabs: {} }));

export function tabTools(tabId: string): TabTools {
  return useDesignTools.getState().tabs[tabId] ?? CLOSED;
}

/** A tab's tool state, re-rendering on change. */
export function useTabTools(tabId: string): TabTools {
  return useDesignTools((state) => state.tabs[tabId] ?? CLOSED);
}

function write(tabId: string, next: (current: TabTools) => TabTools): void {
  useDesignTools.setState((state) => ({
    tabs: { ...state.tabs, [tabId]: next(state.tabs[tabId] ?? CLOSED) },
  }));
}

/** Open `tool` (or close it when it's open), dropping the previous tool's drafts and work. */
export function toggleTool(tabId: string, tool: Tool): void {
  if (tabTools(tabId).tool === tool) closeTool(tabId);
  else openTool(tabId, tool);
}

export function openTool(tabId: string, tool: Tool): void {
  write(tabId, (current) => ({
    ...current,
    ...EMPTY_DRAFTS,
    tool,
    phase: "editing",
    session: current.session + 1,
  }));
}

/** Close the open tool. Always allowed: anything it started is dropped with its session. */
export function closeTool(tabId: string): void {
  if (tabTools(tabId).tool === null) return;
  write(tabId, (current) => ({
    ...current,
    ...EMPTY_DRAFTS,
    tool: null,
    phase: "editing",
    session: current.session + 1,
  }));
}

/**
 * Change the open tool's drafts; false (and no change) unless it's `editing`. Work that
 * finishes later passes the ticket it started with, so it can't land in a newer tool.
 */
export function editDrafts(
  tabId: string,
  change: Partial<Drafts> | ((current: TabTools) => Partial<Drafts>),
  ticket?: Ticket,
): boolean {
  const current = tabTools(tabId);
  if (current.tool === null || current.phase !== "editing") return false;
  if (ticket && !isCurrent(ticket)) return false;
  write(tabId, (latest) => ({
    ...latest,
    ...(typeof change === "function" ? change(latest) : change),
  }));
  return true;
}

/** Async work's claim on the tool session it started in. */
export interface Ticket {
  tabId: string;
  session: number;
}

export function isCurrent(ticket: Ticket): boolean {
  const current = tabTools(ticket.tabId);
  return current.tool !== null && current.session === ticket.session;
}

/** Freeze the open tool for `phase`; null unless it's `editing`. */
export function begin(tabId: string, phase: Exclude<Phase, "editing">): Ticket | null {
  const current = tabTools(tabId);
  if (current.tool === null || current.phase !== "editing") return null;
  write(tabId, (latest) => ({ ...latest, phase }));
  return { tabId, session: current.session };
}

/** End `ticket`'s work and go back to editing; false when its session has passed. */
export function settle(ticket: Ticket, change?: Partial<Drafts>): boolean {
  if (!isCurrent(ticket)) return false;
  write(ticket.tabId, (latest) => ({ ...latest, ...change, phase: "editing" }));
  return true;
}

/** Test hook: every tab closed. */
export function resetDesignTools(): void {
  useDesignTools.setState({ tabs: {} });
}
