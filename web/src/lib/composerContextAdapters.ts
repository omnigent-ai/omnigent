import {
  EMPTY_COMPOSER_CONTEXT,
  normalizeComposerContextState,
  type ComposerContextState,
  type ComposerMcpSelection,
  type ComposerRepositorySelection,
  type WorkingDirectorySelection,
  type WorktreeSelection,
} from "./composerContext";

export interface ComposerContextMetadataV1 {
  version: 1;
  working_directory: null | { path: string };
  worktree:
    | { mode: "none" }
    | { mode: "existing"; path: string; branch: string }
    | { mode: "new"; branch_name: string; base_branch: string | null };
  repositories: { id: string; url: string; branch: string | null }[];
  mcp_context: { id: string; server_name: string }[];
}

export interface ExternalComposerContextCreateFields {
  workspace: string | null;
  git:
    | null
    | { branch_name: string; base_branch?: string }
    | { branch_name: string; existing_worktree: true };
}

export interface ManagedComposerContextCreateFields {
  workspaces: string[];
}

export type ComposerContextCreateAdapterResult =
  | {
      hostType: "external";
      fields: ExternalComposerContextCreateFields;
      metadata: ComposerContextMetadataV1;
    }
  | {
      hostType: "managed";
      fields: ManagedComposerContextCreateFields;
      metadata: ComposerContextMetadataV1;
    };

function workingDirectoryToMetadata(
  selection: WorkingDirectorySelection,
): ComposerContextMetadataV1["working_directory"] {
  return selection.kind === "selected" ? { path: selection.path } : null;
}

function worktreeToMetadata(selection: WorktreeSelection): ComposerContextMetadataV1["worktree"] {
  if (selection.kind === "none") return { mode: "none" };
  if (selection.kind === "existing") {
    return { mode: "existing", path: selection.path, branch: selection.branch };
  }
  return { mode: "new", branch_name: selection.branchName, base_branch: selection.baseBranch };
}

export function composerContextToMetadata(state: ComposerContextState): ComposerContextMetadataV1 {
  const normalized = normalizeComposerContextState(state);
  return {
    version: 1,
    working_directory: workingDirectoryToMetadata(normalized.workingDirectory),
    worktree: worktreeToMetadata(normalized.worktree),
    repositories: normalized.repositories.map((repository) => ({ ...repository })),
    mcp_context: normalized.mcpContext.map(({ id, serverName }) => ({
      id,
      server_name: serverName,
    })),
  };
}

function metadataWorkingDirectory(
  value: ComposerContextMetadataV1["working_directory"],
): WorkingDirectorySelection {
  return value === null ? { kind: "unset" } : { kind: "selected", path: value.path };
}

function metadataWorktree(value: ComposerContextMetadataV1["worktree"]): WorktreeSelection {
  if (value.mode === "none") return { kind: "none" };
  if (value.mode === "existing") {
    return { kind: "existing", path: value.path, branch: value.branch };
  }
  return { kind: "new", branchName: value.branch_name, baseBranch: value.base_branch };
}

export function composerContextFromMetadata(
  metadata: ComposerContextMetadataV1 | null | undefined,
): ComposerContextState {
  if (metadata?.version !== 1) return EMPTY_COMPOSER_CONTEXT;
  return normalizeComposerContextState({
    workingDirectory: metadataWorkingDirectory(metadata.working_directory),
    worktree: metadataWorktree(metadata.worktree),
    repositories: metadata.repositories.map((repository) => ({ ...repository })),
    mcpContext: metadata.mcp_context.map(({ id, server_name }) => ({
      id,
      serverName: server_name,
    })),
  });
}

function repositoryWorkspace(repository: ComposerRepositorySelection): string {
  return repository.branch === null ? repository.url : `${repository.url}#${repository.branch}`;
}

function externalFields(state: ComposerContextState): ExternalComposerContextCreateFields {
  const workspace = state.workingDirectory.kind === "selected" ? state.workingDirectory.path : null;
  if (state.worktree.kind === "none") return { workspace, git: null };
  if (state.worktree.kind === "existing") {
    return {
      workspace: state.worktree.path,
      git: {
        branch_name: state.worktree.branch,
        existing_worktree: true,
      },
    };
  }
  return {
    workspace,
    git: {
      branch_name: state.worktree.branchName,
      ...(state.worktree.baseBranch === null ? {} : { base_branch: state.worktree.baseBranch }),
    },
  };
}

export function composerContextToCreateSession(
  state: ComposerContextState,
  hostType: "external" | "managed",
): ComposerContextCreateAdapterResult {
  const normalized = normalizeComposerContextState(state);
  const metadata = composerContextToMetadata(normalized);
  if (hostType === "external") {
    return { hostType, fields: externalFields(normalized), metadata };
  }
  return {
    hostType,
    fields: { workspaces: normalized.repositories.map(repositoryWorkspace) },
    metadata,
  };
}

export function composerContextFromCreateSession(
  value: ComposerContextCreateAdapterResult,
): ComposerContextState {
  return composerContextFromMetadata(value.metadata);
}

export type ComposerContextSelection = ComposerRepositorySelection | ComposerMcpSelection;
