"use strict";

// Uses the prompt's preload bridge (electron/src/security_key_prompt_preload.js).
document.getElementById("dismiss").addEventListener("click", () => {
  window.omnigentSecurityKeyPrompt.dismiss();
});
