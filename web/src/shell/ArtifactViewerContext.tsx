import { createContext, useContext } from "react";

export interface ArtifactViewerContextValue {
  openArtifact: (entryPath: string) => void;
}

export const ArtifactViewerContext = createContext<ArtifactViewerContextValue | null>(null);

export function useArtifactViewer(): ((entryPath: string) => void) | null {
  return useContext(ArtifactViewerContext)?.openArtifact ?? null;
}
