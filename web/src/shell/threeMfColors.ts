import { strFromU8, strToU8, unzipSync, zipSync } from "three/examples/jsm/libs/fflate.module.js";

const CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02";
const PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06";
const MODEL_RELATIONSHIP = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel";
const MAX_SELECTED_BYTES = 32 * 1024 * 1024;
const MAX_OBJECTS = 10_000;
const MAX_COMPONENTS = 10_000;
const MAX_COMPONENT_DEPTH = 64;
const APPEARANCE = new Set([
  "basematerials",
  "colorgroup",
  "texture2d",
  "texture2dgroup",
  "compositematerials",
  "multiproperties",
  "metallicdisplayproperties",
  "pbmetallicdisplayproperties",
]);

const children = (element: Element, name: string) =>
  Array.from(element.children).filter((child) => child.localName === name);
const metadata = (element: Element | undefined, key: string) =>
  element &&
  children(element, "metadata")
    .find((child) => child.getAttribute("key") === key)
    ?.getAttribute("value");
const normal = (part: Element) =>
  !part.getAttribute("subtype") || part.getAttribute("subtype") === "normal_part";

function slot(element: Element | undefined, inherited: number | null): number | null {
  const value = metadata(element, "extruder");
  if (value == null) return inherited;
  if (!/^\d+$/.test(value)) return null;
  const number = Number(value);
  if (number === 0) return inherited;
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

function xml(bytes: Uint8Array): Document {
  const document = new DOMParser().parseFromString(strFromU8(bytes), "application/xml");
  if (document.getElementsByTagName("parsererror").length) throw new Error("Invalid XML");
  return document;
}

function zipPath(path: string, containing = ""): string {
  const segments = (
    path.startsWith("/") ? path : containing.slice(0, containing.lastIndexOf("/") + 1) + path
  ).split("/");
  const result: string[] = [];
  for (const segment of segments) {
    if (segment === "..") {
      if (!result.length) throw new Error("Invalid model path");
      result.pop();
    } else if (segment && segment !== ".") result.push(segment);
  }
  return result.join("/");
}

export function applyThreeMfColors(buffer: ArrayBuffer): ArrayBuffer {
  try {
    let selectedBytes = 0;
    const select = (size: number) => {
      selectedBytes += size;
      if (selectedBytes > MAX_SELECTED_BYTES) throw new Error("3MF byte limit");
      return true;
    };
    const bytes = new Uint8Array(buffer);
    const configs = unzipSync(bytes, {
      filter: (entry) =>
        (entry.name === "Metadata/model_settings.config" ||
          entry.name === "Metadata/project_settings.config") &&
        select(entry.originalSize),
    });
    if (!configs["Metadata/model_settings.config"] || !configs["Metadata/project_settings.config"])
      return buffer;
    const palette: unknown = JSON.parse(
      strFromU8(configs["Metadata/project_settings.config"]),
    ).filament_colour;
    if (!Array.isArray(palette)) return buffer;
    const color = (index: number | null): string | null => {
      const value: unknown = index === null ? null : palette[index - 1];
      return typeof value === "string" && /^#[\da-f]{6}([\da-f]{2})?$/i.test(value)
        ? value.slice(0, 7).toUpperCase()
        : null;
    };
    const settings = xml(configs["Metadata/model_settings.config"]);
    const objectConfigs = new Map(
      children(settings.documentElement, "object").map((object) => [
        object.getAttribute("id"),
        object,
      ]),
    );
    const candidateColors = new Set<string>();
    for (const object of objectConfigs.values()) {
      for (const part of children(object, "part")) {
        const effective = normal(part) ? color(slot(part, slot(object, 1))) : null;
        if (effective) candidateColors.add(effective);
      }
    }
    if (candidateColors.size < 2) return buffer;

    const entries = unzipSync(bytes, {
      filter: (entry) =>
        (entry.name.endsWith(".model") || entry.name === "_rels/.rels") &&
        select(entry.originalSize),
    });
    const rootRelationship = Array.from(
      xml(entries["_rels/.rels"]).getElementsByTagName("Relationship"),
    ).find((relationship) => relationship.getAttribute("Type") === MODEL_RELATIONSHIP);
    if (!rootRelationship) return buffer;
    const rootPath = zipPath(rootRelationship.getAttribute("Target") ?? "");
    const models = new Map<string, Document>();
    const objects = new Map<string, Element>();
    let objectCount = 0;
    let componentCount = 0;
    for (const [path, data] of Object.entries(entries)) {
      if (!path.endsWith(".model")) continue;
      const document = xml(data);
      for (const element of document.querySelectorAll("*")) {
        if (
          APPEARANCE.has(element.localName) ||
          ((element.localName === "object" || element.localName === "triangle") &&
            element.hasAttribute("pid"))
        )
          return buffer;
        if (element.localName === "component" && ++componentCount > MAX_COMPONENTS) return buffer;
      }
      const resources = children(document.documentElement, "resources")[0];
      if (!resources) return buffer;
      const normalizedPath = zipPath(path);
      if (models.has(normalizedPath)) return buffer;
      models.set(normalizedPath, document);
      for (const object of children(resources, "object")) {
        if (++objectCount > MAX_OBJECTS) return buffer;
        const key = JSON.stringify([normalizedPath, object.getAttribute("id")]);
        if (objects.has(key)) return buffer;
        objects.set(key, object);
      }
    }
    const root = models.get(rootPath);
    if (!root) return buffer;
    const output = document.implementation.createDocument(CORE, "model");
    const outputModel = output.documentElement;
    if (root.documentElement.hasAttribute("unit"))
      outputModel.setAttribute("unit", root.documentElement.getAttribute("unit")!);
    const resources = output.createElementNS(CORE, "resources");
    const bases = output.createElementNS(CORE, "basematerials");
    bases.setAttribute("id", "1");
    resources.append(bases);
    outputModel.append(resources);
    const colors: string[] = [];
    const memo = new Map<string, string>();
    const visiting = new Set<string>();
    let nextId = 2;
    let visits = 0;
    const clone = (
      path: string,
      id: string,
      parts: Map<string | null, Element>,
      inherited: number | null,
      assigned: Element | undefined,
      enabled: boolean,
      depth: number,
    ): string => {
      if (depth > MAX_COMPONENT_DEPTH || ++visits > MAX_COMPONENTS)
        throw new Error("3MF component limit");
      const sourceKey = JSON.stringify([path, id]);
      const source = objects.get(sourceKey);
      if (!source || visiting.has(sourceKey)) throw new Error("Invalid component graph");
      visiting.add(sourceKey);
      const effectiveSlot = slot(assigned, inherited);
      const recolor = enabled && (!assigned || normal(assigned));
      const object = output.importNode(source, true);
      const mesh = children(object, "mesh")[0];
      const effectiveColor = mesh && assigned && recolor ? color(effectiveSlot) : null;
      for (const components of children(object, "components")) {
        for (const child of children(components, "component")) {
          const childId = child.getAttribute("objectid") ?? "";
          const targetPath = child.getAttributeNS(PRODUCTION, "path");
          child.setAttribute(
            "objectid",
            clone(
              targetPath ? zipPath(targetPath, path) : path,
              childId,
              parts,
              effectiveSlot,
              parts.get(childId) ?? assigned,
              recolor,
              depth + 1,
            ),
          );
          child.removeAttributeNS(PRODUCTION, "path");
        }
      }
      visiting.delete(sourceKey);
      const key = JSON.stringify([
        sourceKey,
        effectiveColor,
        mesh ? null : new XMLSerializer().serializeToString(object),
      ]);
      const previous = memo.get(key);
      if (previous) return previous;
      const newId = String(nextId++);
      memo.set(key, newId);
      object.setAttribute("id", newId);
      if (effectiveColor) {
        let index = colors.indexOf(effectiveColor);
        if (index < 0) {
          index = colors.length;
          colors.push(effectiveColor);
          const base = output.createElementNS(CORE, "base");
          base.setAttribute("name", effectiveColor);
          base.setAttribute("displaycolor", effectiveColor);
          bases.append(base);
        }
        object.setAttribute("pid", "1");
        object.setAttribute("pindex", String(index));
      }
      resources.append(object);
      return newId;
    };
    const sourceBuild = children(root.documentElement, "build")[0];
    if (!sourceBuild) return buffer;
    const build = output.importNode(sourceBuild, true);
    for (const item of children(build, "item")) {
      const id = item.getAttribute("objectid") ?? "";
      const config = objectConfigs.get(id);
      const parts = new Map(
        config ? children(config, "part").map((part) => [part.getAttribute("id"), part]) : [],
      );
      item.setAttribute(
        "objectid",
        clone(rootPath, id, parts, slot(config, 1), parts.get(id), true, 0),
      );
    }
    if (colors.length < 2) return buffer;
    outputModel.append(build);
    const relationships = `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="r" Type="${MODEL_RELATIONSHIP}"/></Relationships>`;
    return zipSync(
      {
        "_rels/.rels": new Uint8Array(strToU8(relationships)),
        "3D/3dmodel.model": new Uint8Array(strToU8(new XMLSerializer().serializeToString(output))),
      },
      { level: 0 },
    ).buffer as ArrayBuffer;
  } catch {
    return buffer;
  }
}
