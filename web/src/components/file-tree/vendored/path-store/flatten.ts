import { hasNodeFlag, isDirectoryNode, PATH_STORE_NODE_FLAG_ROOT } from "./internal-types";
import type { NodeId } from "./internal-types";
import type { PathStoreState } from "./state";

// Fully known trees flatten single-child directory chains even before callers
// explicitly expand the intermediate folders. That keeps rows like
// `config/project/` visible on first render instead of requiring a priming click.
export function getFlattenedChildDirectoryId(
  state: PathStoreState,
  directoryNodeId: NodeId,
): NodeId | null {
  if (state.snapshot.options.flattenEmptyDirectories !== true) {
    return null;
  }

  const directoryNode = state.snapshot.nodes[directoryNodeId];
  if (
    directoryNode == null ||
    !isDirectoryNode(directoryNode) ||
    hasNodeFlag(directoryNode, PATH_STORE_NODE_FLAG_ROOT)
  ) {
    return null;
  }

  const directoryIndex = state.snapshot.directories.get(directoryNodeId);
  if (directoryIndex == null || directoryIndex.childIds.length !== 1) {
    return null;
  }

  const childId = directoryIndex.childIds[0];
  if (childId == null) {
    return null;
  }

  const childNode = state.snapshot.nodes[childId];
  if (childNode == null || !isDirectoryNode(childNode)) {
    return null;
  }

  return childId;
}

export function getFlattenedTerminalDirectoryId(
  state: PathStoreState,
  directoryNodeId: NodeId,
): NodeId {
  let currentDirectoryId = directoryNodeId;

  while (true) {
    const nextDirectoryId = getFlattenedChildDirectoryId(state, currentDirectoryId);
    if (nextDirectoryId == null) {
      return currentDirectoryId;
    }

    currentDirectoryId = nextDirectoryId;
  }
}

export function collectFlattenedDirectoryChainIds(
  state: PathStoreState,
  directoryNodeId: NodeId,
): NodeId[] {
  const chainIds = [directoryNodeId];
  let currentDirectoryId = directoryNodeId;

  while (true) {
    const nextDirectoryId = getFlattenedChildDirectoryId(state, currentDirectoryId);
    if (nextDirectoryId == null) {
      return chainIds;
    }

    chainIds.push(nextDirectoryId);
    currentDirectoryId = nextDirectoryId;
  }
}
