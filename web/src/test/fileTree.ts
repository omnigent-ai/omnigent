// Reading and driving a rendered file tree in tests by its rows' paths and names,
// which its virtualized rows carry as attributes.

import { waitFor } from "@testing-library/react";
import { FILE_TREE_ROOT_SELECTOR } from "@/components/file-tree/vendored";

/** The rendered tree element; with several on the page, the first inside `within`. */
export function treeHost(within: ParentNode = document): HTMLElement {
  const host = within.querySelector<HTMLElement>(FILE_TREE_ROOT_SELECTOR);
  if (!host) throw new Error("No rendered file tree");
  return host;
}

/** Every rendered row, top to bottom. */
export function treeRows(within: ParentNode = document): HTMLElement[] {
  return [...treeHost(within).querySelectorAll<HTMLElement>('[role="treeitem"][data-item-path]')];
}

/** The rows' paths, top to bottom; folders end in "/". */
export function treeRowPaths(within: ParentNode = document): string[] {
  return treeRows(within).map((row) => row.getAttribute("data-item-path")!);
}

/** The row for `path`, once it has rendered. */
export async function findTreeRow(
  path: string,
  within: ParentNode = document,
): Promise<HTMLElement> {
  let found: HTMLElement | undefined;
  await waitFor(() => {
    found = treeRows(within).find((row) => row.getAttribute("data-item-path") === path);
    if (!found) throw new Error(`No row for ${path}; rows: ${treeRowPaths(within).join(", ")}`);
  });
  return found!;
}

/** The row whose accessible name is `name` (a merged chain reads "a / b"), once it renders. */
export async function findTreeRowNamed(
  name: string,
  within: ParentNode = document,
): Promise<HTMLElement> {
  let found: HTMLElement | undefined;
  await waitFor(() => {
    found = treeRows(within).find((row) => row.getAttribute("aria-label") === name);
    if (!found) {
      const names = treeRows(within).map((row) => row.getAttribute("aria-label"));
      throw new Error(`No row named ${name}; rows: ${names.join(", ")}`);
    }
  });
  return found!;
}

/** The row named `name`, if it has rendered. */
export function queryTreeRowNamed(name: string, within: ParentNode = document): HTMLElement | null {
  return treeRows(within).find((row) => row.getAttribute("aria-label") === name) ?? null;
}
