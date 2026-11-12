/**
 * The user's saved workflows, for the Workflows page.
 *
 * Sample data for now, returned in a query's shape (`data`, `isLoading`,
 * `isError`, `refetch`), so reading `GET /v1/workflows` can replace this module
 * without touching the page.
 */

/** A saved workflow as a list shows it: `GET /v1/workflows` returns this shape. */
export interface WorkflowSummary {
  /** A bare 32-character hex ID. */
  id: string;
  /** One line, unique among the owner's workflows. */
  name: string;
  /** What the workflow does and what a run needs. */
  description: string;
  /** Unix epoch seconds when it was saved. */
  created_at: number;
  /** Unix epoch seconds of the last change. */
  updated_at: number;
}

const HOUR_S = 60 * 60;
const DAY_S = 24 * HOUR_S;

function sampleWorkflows(nowS: number): WorkflowSummary[] {
  const sample = (
    id: string,
    name: string,
    description: string,
    ageS: number,
  ): WorkflowSummary => ({
    id,
    name,
    description,
    created_at: nowS - ageS - 2 * DAY_S,
    updated_at: nowS - ageS,
  });
  return [
    sample(
      "6f1c2b9a4d8e4f30a1b2c3d4e5f60718",
      "Fix flaky tests",
      "Runs the suite a few times, files an issue for each test that fails only sometimes, and fixes the clear ones in a PR each. Give it: the suite to run and how many runs.",
      2 * HOUR_S,
    ),
    sample(
      "8a7b6c5d4e3f42019f8e7d6c5b4a3921",
      "Release a new version",
      "Cuts the release branch, runs the checks, tags the release and drafts the notes. Give it: the version number.",
      3 * DAY_S,
    ),
    sample(
      "1d2c3b4a59684f7e8d9c0b1a2f3e4d5c",
      "Remove a deprecated feature in small PRs",
      "Finds every reference to the feature, splits the removal into small PRs, and opens them in order. Give it: the feature and its tracking issue.",
      8 * DAY_S,
    ),
    sample(
      "c0ffee00112233445566778899aabbcc",
      "Upgrade a dependency",
      "Bumps one dependency, fixes what breaks, and opens a PR with the changes. Give it: the package and the version to move to.",
      36 * DAY_S,
    ),
  ];
}

// Fixed when the module loads, so each render returns the same objects.
const SAMPLE_WORKFLOWS = sampleWorkflows(Math.floor(Date.now() / 1000));

export interface WorkflowsQuery {
  /** The workflows by name, or `undefined` until they load. */
  data: WorkflowSummary[] | undefined;
  isLoading: boolean;
  isError: boolean;
  refetch: () => void;
}

/** The user's saved workflows, sorted by name. */
export function useWorkflows(): WorkflowsQuery {
  return { data: SAMPLE_WORKFLOWS, isLoading: false, isError: false, refetch: () => {} };
}
