// Saving and opening team files (YAML) in the browser.

/** Save text as a file through the browser's download. */
export function downloadText(fileName: string, text: string, type = "application/yaml"): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** Let the PO pick a team file; resolves to its text, or null when cancelled. */
export function pickTextFile(accept = ".yaml,.yml,application/yaml,text/yaml"): Promise<string | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = accept;
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      if (!file) resolve(null);
      else void file.text().then(resolve, () => resolve(null));
    });
    input.addEventListener("cancel", () => resolve(null));
    input.click();
  });
}
