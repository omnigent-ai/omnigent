// Pure helpers for `*.wireframe.html`: device sizes, the screen list, and the
// srcdoc with its frame script. Each top-level `<section data-screen>` is a
// screen; a file without them is one screen.

import { injectDesignDoc } from "./codeViewerHelpers";

/** Tags every postMessage between WireframeViewer and its iframe. */
export const WIREFRAME_MSG_SOURCE = "omnigent-wireframe";

export const WIREFRAME_DEVICES = [
  { id: "desktop", label: "Desktop", width: 1440, height: 900 },
  { id: "tablet", label: "Tablet", width: 834, height: 1194 },
  { id: "phone", label: "Phone", width: 390, height: 844 },
] as const;

export type WireframeDevice = (typeof WIREFRAME_DEVICES)[number];

export interface WireframeScreen {
  id: string;
  title: string;
}

export function isWireframeFile(path: string): boolean {
  return path.toLowerCase().endsWith(".wireframe.html");
}

/** Screens in source order, first of each id wins. DOMParser documents are inert. */
export function listWireframeScreens(html: string): WireframeScreen[] {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const screens = new Map<string, WireframeScreen>();
  for (const section of doc.querySelectorAll("body > section[data-screen]")) {
    const id = section.getAttribute("data-screen")?.trim() ?? "";
    if (!id || screens.has(id)) continue;
    screens.set(id, { id, title: section.getAttribute("data-title")?.trim() || id });
  }
  return [...screens.values()];
}

// Only the active screen shows; a file without screens shows as is.
const WIREFRAME_STYLE = `<style>
@media screen{body>section[data-screen]:not([data-omnigent-active]){display:none!important}}
</style>`;

// Links to `#id` and `data-goto` would open a new tab under the preview's
// `<base target="_blank">`, so the script handles them in the capture phase.
const WIREFRAME_SCRIPT = `<script>
(function(){
var S="${WIREFRAME_MSG_SOURCE}";
function all(){return document.querySelectorAll("body > section[data-screen]");}
function find(id){var s=all();for(var k=0;k<s.length;k++)if(s[k].getAttribute("data-screen").trim()===id)return s[k];return null;}
function show(id){var s=all(),t=find(id)||s[0];for(var k=0;k<s.length;k++)s[k].toggleAttribute("data-omnigent-active",s[k]===t);scrollTo(0,0);}
show(null);
addEventListener("message",function(e){
if(e.source!==parent||!e.data||e.data.source!==S)return;
if(e.data.type==="goto"&&typeof e.data.id==="string")show(e.data.id);
});
addEventListener("click",function(e){
var el=e.target&&e.target.closest&&e.target.closest("[data-goto], a[href^='#']");
if(!el)return;
e.preventDefault();
var id=(el.hasAttribute("data-goto")?el.getAttribute("data-goto"):el.getAttribute("href").slice(1)).trim();
if(id&&find(id)){show(id);parent.postMessage({source:S,type:"screen",id:id},"*");return;}
var t=id&&document.getElementById(id);
if(t)t.scrollIntoView();
},true);
})();
</script>`;

/** The wireframe srcdoc; styles go where `prepareSlidesDoc` puts them. */
export function prepareWireframeDoc(html: string, kitStyle = "", systemStyle = ""): string {
  return injectDesignDoc(html, systemStyle, WIREFRAME_STYLE + kitStyle + WIREFRAME_SCRIPT);
}
