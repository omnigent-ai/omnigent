export interface CreationAttempt {
  key: string;
  signature: string;
  files: readonly File[];
}

/** A retry keeps its key; editing the submitted choices starts a new operation. */
export function reuseCreationAttempt(
  previous: CreationAttempt | undefined,
  payload: unknown,
  files: readonly File[] = [],
): CreationAttempt {
  const signature = JSON.stringify(payload);
  if (
    previous?.signature === signature &&
    previous.files.length === files.length &&
    previous.files.every((file, index) => file === files[index])
  )
    return previous;
  return { key: crypto.randomUUID(), signature, files: [...files] };
}
