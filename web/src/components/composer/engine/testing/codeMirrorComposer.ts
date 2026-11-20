// The CodeMirror composer input as the loader builds it, for tests that render it directly.

import { createCodeMirrorComposerInput } from "../CodeMirrorComposerInput";
import { codeMirrorComposerEnvironment } from "../codeMirrorComposerLoader";

export const CodeMirrorComposerInput = createCodeMirrorComposerInput(
  codeMirrorComposerEnvironment(),
);
