// A repository's labels as the label picker lists them.

/** One label as the picker lists it. */
export interface RepositoryLabel {
  name: string;
  /** Six hex digits, lowercase, or null when GitHub's isn't one. */
  color: string | null;
  description: string | null;
}

/** The repository's labels as the picker reads them. */
export interface RepositoryLabels {
  object: "list";
  /** Sorted by name. */
  data: RepositoryLabel[];
  /** The repository has more labels than one read lists. */
  has_more: boolean;
}
