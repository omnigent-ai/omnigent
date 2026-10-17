import { EditorWorker } from "monaco-editor/esm/vs/editor/common/services/editorWebWorker.js";

const model = (lines) => ({
  getLinesContent: () => lines,
  getLineCount: () => lines.length,
  getLineContent: (line) => lines[line - 1],
});

self.onmessage = ({ data: { before, after, options, algorithm } }) => {
  self.postMessage(EditorWorker.computeDiff(model(before), model(after), options, algorithm));
};
