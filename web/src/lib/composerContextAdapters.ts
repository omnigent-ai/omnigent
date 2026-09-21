import {
  EMPTY_COMPOSER_CONTEXT,
  normalizeComposerContextState,
  type ComposerContextState,
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
}

export const COMPOSER_CONTEXT_LABEL_KEY = "omnigent.composer_context.v1";
const COMPOSER_CONTEXT_LABEL_CHUNK_SIZE = 240;

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
  });
}

export function composerContextToLabel(state: ComposerContextState): string {
  return JSON.stringify(composerContextToMetadata(state));
}

export function composerContextToLabels(state: ComposerContextState): Record<string, string> {
  const serialized = composerContextToLabel(state);
  const labels: Record<string, string> = {};
  for (
    let offset = 0, index = 0;
    offset < serialized.length;
    offset += COMPOSER_CONTEXT_LABEL_CHUNK_SIZE, index += 1
  ) {
    labels[`${COMPOSER_CONTEXT_LABEL_KEY}.${index}`] = serialized.slice(
      offset,
      offset + COMPOSER_CONTEXT_LABEL_CHUNK_SIZE,
    );
  }
  return labels;
}

export function composerContextFromLabels(
  labels: Record<string, string> | null | undefined,
): ComposerContextState {
  const raw =
    labels?.[COMPOSER_CONTEXT_LABEL_KEY] ??
    Object.entries(labels ?? {})
      .filter(([key]) => key.startsWith(`${COMPOSER_CONTEXT_LABEL_KEY}.`))
      .sort(([left], [right]) => {
        const leftIndex = Number(left.slice(COMPOSER_CONTEXT_LABEL_KEY.length + 1));
        const rightIndex = Number(right.slice(COMPOSER_CONTEXT_LABEL_KEY.length + 1));
        return leftIndex - rightIndex;
      })
      .map(([, value]) => value)
      .join("");
  if (!raw) return EMPTY_COMPOSER_CONTEXT;
  try {
    return composerContextFromMetadata(JSON.parse(raw) as ComposerContextMetadataV1);
  } catch {
    return EMPTY_COMPOSER_CONTEXT;
  }
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

export type ComposerContextSelection = ComposerRepositorySelection;
