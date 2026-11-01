// Per-device opt-in to show images that tool calls read or return under their
// tool rows in the chat. Off by default, since screenshots take room.

import { createBooleanPreference } from "./booleanPreference";

export const SHOW_TOOL_RESULT_IMAGES_STORAGE_KEY = "omnigent:show-tool-result-images";

export const DEFAULT_SHOW_TOOL_RESULT_IMAGES = false;

const preference = createBooleanPreference(
  SHOW_TOOL_RESULT_IMAGES_STORAGE_KEY,
  DEFAULT_SHOW_TOOL_RESULT_IMAGES,
);

export const readShowToolResultImages = preference.read;
export const writeShowToolResultImages = preference.write;
export const useShowToolResultImages = preference.use;
