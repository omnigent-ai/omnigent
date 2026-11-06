import { afterEach, expect, it, vi } from "vitest";
import { openSameOriginSessionLink } from "./sessionNavigation";

const ORIGIN = window.location.origin;

afterEach(() => {
  delete window.__OMNIGENT_BASE_PATH__;
  window.history.replaceState(null, "", "/");
});

it.each([
  ["", "/c/conv-2", "/c/conv-2"],
  ["", "/canvas/c/conv-2", "/canvas/c/conv-2"],
  ["/proxy/6767", "/proxy/6767/canvas/c/conv-2", "/proxy/6767/canvas/c/conv-2"],
  ["/proxy/6767", "/c/conv-2", "/proxy/6767/c/conv-2"],
])("opens a session route in place (base %j, link %s)", (base, path, expected) => {
  window.__OMNIGENT_BASE_PATH__ = base;
  const onPopState = vi.fn();
  window.addEventListener("popstate", onPopState);
  expect(openSameOriginSessionLink(`${ORIGIN}${path}`)).toBe(true);
  window.removeEventListener("popstate", onPopState);
  expect(window.location.pathname).toBe(expected);
  expect(onPopState).toHaveBeenCalledOnce();
});

it.each([
  ["", "/settings/c/session"],
  ["", "/v1/sessions/conv-1/c/x"],
  ["", "/c/conv-2/extra"],
  ["", "https://other.example.com/c/conv-2"],
  ["/proxy/6767", "/proxy/6767/settings/c/session"],
])("leaves a non-session link alone (base %j, link %s)", (base, link) => {
  window.__OMNIGENT_BASE_PATH__ = base;
  expect(openSameOriginSessionLink(new URL(link, ORIGIN).href)).toBe(false);
  expect(window.location.pathname).toBe("/");
});
