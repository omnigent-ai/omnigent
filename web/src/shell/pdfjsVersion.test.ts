import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);

describe("pdf.js worker version", () => {
  it("matches the pdfjs-dist version react-pdf runs", () => {
    // PdfViewer builds the worker from the app's pdfjs-dist; pdf.js rejects a mismatched worker.
    const app = require("../../package.json") as { dependencies: Record<string, string> };
    const reactPdf = require("react-pdf/package.json") as {
      dependencies: Record<string, string>;
    };
    expect(app.dependencies["pdfjs-dist"]).toBe(reactPdf.dependencies["pdfjs-dist"]);
  });
});
