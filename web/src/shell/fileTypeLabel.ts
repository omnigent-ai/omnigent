import { CODE_BLOCK_LANGUAGES } from "./codeBlockLanguages";
import { detectLang, isImageFile, isModelFile, isPdfFile } from "./codeViewerHelpers";

const LANGUAGE_LABELS = new Map(CODE_BLOCK_LANGUAGES.map(({ value, label }) => [value, label]));

export function fileTypeLabel(name: string, kind: "file" | "folder"): string {
  if (kind === "folder") return "Folder";

  const basename = name.split(/[\\/]/).pop() ?? name;
  const dot = basename.lastIndexOf(".");
  const extension = dot > 0 && dot < basename.length - 1 ? basename.slice(dot + 1) : "";
  if (!extension) return "File";

  const path = `file.${extension}`;
  const suffix = `(.${extension.toLowerCase()})`;
  if (isModelFile(path)) {
    return `${extension.toUpperCase()} 3D model ${suffix}`;
  }
  if (isImageFile(path)) return `${extension.toUpperCase()} image ${suffix}`;
  if (isPdfFile(path)) return `PDF document ${suffix}`;

  const language = detectLang(path);
  const label = LANGUAGE_LABELS.get(language);
  return label ? `${label} file ${suffix}` : `File ${suffix}`;
}
