"use strict";

const { dialog } = require("electron");

const original = dialog.showMessageBox.bind(dialog);
global.previewPrompts = [];
global.answerPreviewPrompt = null;
dialog.showMessageBox = (parent, options) => {
  if (!options.message?.startsWith("Let sessions on ")) return original(parent, options);
  global.previewPrompts.push(options);
  if (process.env.OMNIGENT_DESKTOP_NATIVE_DIALOGS === "1") return original(parent, options);
  return new Promise((resolve) => {
    global.answerPreviewPrompt = (response) => {
      global.answerPreviewPrompt = null;
      resolve({ response });
    };
  });
};
