import { strFromU8, strToU8, zipSync, Inflate } from "three/examples/jsm/libs/fflate.module.js";

const CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02";
const XMLNS = "http://www.w3.org/2000/xmlns/";
const PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06";
const MODEL_RELATIONSHIP = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel";
export const MAX_SELECTED_BYTES = 128 * 1024 * 1024;
export const INFLATE_CHUNK_BYTES = 16 * 1024;
const MAX_EMITTED_RATIO = 2;
const MAX_EMITTED_OVERHEAD = 1024 * 1024;
const MAX_OBJECTS = 10_000;
const MAX_COMPONENTS = 10_000;
const MAX_COMPONENT_DEPTH = 64;
const ZIP_END = {
  signature: 0x06054b50,
  headerSize: 22,
  diskNumbers: 4,
  diskEntries: 8,
  entries: 10,
  directorySize: 12,
  directoryOffset: 16,
  commentLength: 20,
};
const ZIP_DIRECTORY = {
  signature: 0x02014b50,
  headerSize: 46,
  flags: 8,
  compression: 10,
  compressedSize: 20,
  originalSize: 24,
  nameLength: 28,
  extraLength: 30,
  commentLength: 32,
  diskNumber: 34,
  localOffset: 42,
};
const ZIP_LOCAL = {
  signature: 0x04034b50,
  headerSize: 30,
  flags: 6,
  compression: 8,
  compressedSize: 18,
  originalSize: 22,
  nameLength: 26,
  extraLength: 28,
};
const ZIP_FLAGS = { allowed: 0x080e, utf8: 0x0800, descriptor: 0x0008 };
const ZIP_MAX_16BIT = 0xffff;
const ZIP64_SIZE = 0xffffffff;
const ZIP_STORED = 0;
const ZIP_DEFLATED = 8;
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

function utf8Length(text: string): number {
  let bytes = text.length;
  for (let index = 0; index < text.length; index++) {
    const code = text.charCodeAt(index);
    if (code >= 0x800) {
      bytes += 2;
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = text.charCodeAt(index + 1);
        if (next >= 0xdc00 && next <= 0xdfff) index++;
      }
    } else if (code >= 0x80) bytes++;
  }
  return bytes;
}

function boundedSerialization(source: Element, limit: number): string {
  const escapedBytes = (text: string, attribute = false) => {
    let size = utf8Length(text);
    for (let index = 0; index < text.length; index++) {
      const code = text.charCodeAt(index);
      if (code === 38) size += 4;
      else if (code === 60 || code === 62) size += 3;
      else if (attribute && code === 34) size += 5;
      else if (attribute && (code === 9 || code === 10 || code === 13)) size += 5;
    }
    return size;
  };
  let bound = 0;
  const namespaceSizes = new Map<string, number>();
  const namespace = (element: Element, prefix: string | null, uri: string | null) => {
    if (!uri || uri === XMLNS || prefix === "xml") return;
    const name = prefix ? `xmlns:${prefix}` : "xmlns";
    for (let ancestor: Element | null = element; ancestor; ancestor = ancestor.parentElement) {
      if (ancestor.getAttributeNS(XMLNS, prefix || "xmlns") === uri) return;
      if (ancestor !== element && ancestor.prefix === prefix && ancestor.namespaceURI === uri)
        return;
      if (ancestor === source) break;
    }
    let size = namespaceSizes.get(uri);
    if (size === undefined) {
      size = escapedBytes(uri, true);
      namespaceSizes.set(uri, size);
    }
    bound += utf8Length(name) + size + 4;
  };
  // Standalone serialization can repeat namespace bindings inherited outside this subtree.
  const walker = source.ownerDocument.createTreeWalker(source, NodeFilter.SHOW_ALL);
  do {
    const node = walker.currentNode;
    if (node instanceof Element) {
      bound += 2 * utf8Length(node.tagName) + 5;
      namespace(node, node.prefix, node.namespaceURI);
      for (const name of node.getAttributeNames()) {
        bound += utf8Length(name) + escapedBytes(node.getAttribute(name) ?? "", true) + 4;
        const separator = name.indexOf(":");
        if (separator >= 0 && !name.startsWith("xmlns:")) {
          const prefix = name.slice(0, separator);
          namespace(node, prefix, node.lookupNamespaceURI(prefix));
        }
      }
    } else {
      bound += escapedBytes(node.nodeValue ?? "");
      if (node.nodeType !== Node.TEXT_NODE) bound += utf8Length(node.nodeName) + 16;
    }
    if (bound > limit) throw new Error("3MF serialization byte limit");
  } while (walker.nextNode());
  return new XMLSerializer().serializeToString(source);
}

function xml(bytes: Uint8Array): Document {
  const text = strFromU8(bytes);
  let offset = 0;
  while (offset < text.length) {
    if (/\s/.test(text[offset])) {
      offset++;
      continue;
    }
    const end = text.startsWith("<!--", offset)
      ? "-->"
      : text.startsWith("<?", offset)
        ? "?>"
        : null;
    if (!end) {
      if (text.slice(offset, offset + 9).toUpperCase() === "<!DOCTYPE")
        throw new Error("Unsupported XML doctype");
      break;
    }
    const next = text.indexOf(end, offset + 2);
    if (next === -1) throw new Error("Invalid XML prolog");
    offset = next + end.length;
  }
  const document = new DOMParser().parseFromString(text, "application/xml");
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
    const bytes = new Uint8Array(buffer);
    const view = new DataView(buffer);
    const read16 = (offset: number) => view.getUint16(offset, true);
    const read32 = (offset: number) => view.getUint32(offset, true);
    // unzipSync allocates from declared sizes, so it cannot bound real inflate work.
    const floor = Math.max(0, bytes.length - ZIP_END.headerSize - ZIP_MAX_16BIT);
    let end = bytes.length - ZIP_END.headerSize;
    for (; end >= floor; end--)
      if (
        read32(end) === ZIP_END.signature &&
        end + ZIP_END.headerSize + read16(end + ZIP_END.commentLength) === bytes.length
      )
        break;
    if (
      end < floor ||
      read32(end + ZIP_END.diskNumbers) !== 0 ||
      read16(end + ZIP_END.diskEntries) !== read16(end + ZIP_END.entries) ||
      read16(end + ZIP_END.entries) === ZIP_MAX_16BIT
    )
      throw new Error("Unsupported ZIP directory");
    const directory = read32(end + ZIP_END.directoryOffset);
    if (directory + read32(end + ZIP_END.directorySize) !== end)
      throw new Error("Invalid ZIP directory");
    let selectedBytes = 0;
    let extractedBytes = 0;
    const extract = (needed: (name: string) => boolean) => {
      const ranges = new Map<
        string,
        { start: number; size: number; originalSize: number; compression: number }
      >();
      let offset = directory;
      for (let index = 0; index < read16(end + ZIP_END.entries); index++) {
        if (offset + ZIP_DIRECTORY.headerSize > end || read32(offset) !== ZIP_DIRECTORY.signature)
          throw new Error("Invalid ZIP entry");
        const record = offset;
        const flags = read16(record + ZIP_DIRECTORY.flags);
        const nameLength = read16(record + ZIP_DIRECTORY.nameLength);
        offset +=
          ZIP_DIRECTORY.headerSize +
          nameLength +
          read16(record + ZIP_DIRECTORY.extraLength) +
          read16(record + ZIP_DIRECTORY.commentLength);
        if (
          offset > end ||
          flags & ~ZIP_FLAGS.allowed ||
          read16(record + ZIP_DIRECTORY.diskNumber) !== 0
        )
          throw new Error("Unsupported ZIP entry");
        const name = strFromU8(
          bytes.subarray(
            record + ZIP_DIRECTORY.headerSize,
            record + ZIP_DIRECTORY.headerSize + nameLength,
          ),
          !(flags & ZIP_FLAGS.utf8),
        );
        if (!needed(name)) continue;
        const size = read32(record + ZIP_DIRECTORY.compressedSize);
        const originalSize = read32(record + ZIP_DIRECTORY.originalSize);
        const compression = read16(record + ZIP_DIRECTORY.compression);
        const local = read32(record + ZIP_DIRECTORY.localOffset);
        if (
          size === ZIP64_SIZE ||
          originalSize === ZIP64_SIZE ||
          (compression !== ZIP_STORED && compression !== ZIP_DEFLATED) ||
          (compression === ZIP_STORED && size !== originalSize)
        )
          throw new Error("Invalid ZIP size");
        if (
          local + ZIP_LOCAL.headerSize > directory ||
          read32(local) !== ZIP_LOCAL.signature ||
          read16(local + ZIP_LOCAL.flags) !== flags ||
          read16(local + ZIP_LOCAL.compression) !== compression
        )
          throw new Error("Inconsistent ZIP header");
        const start =
          local +
          ZIP_LOCAL.headerSize +
          read16(local + ZIP_LOCAL.nameLength) +
          read16(local + ZIP_LOCAL.extraLength);
        if (
          start + size > directory ||
          strFromU8(
            bytes.subarray(
              local + ZIP_LOCAL.headerSize,
              local + ZIP_LOCAL.headerSize + read16(local + ZIP_LOCAL.nameLength),
            ),
            !(flags & ZIP_FLAGS.utf8),
          ) !== name
        )
          throw new Error("Invalid ZIP range");
        for (const [field, expected] of [
          [ZIP_LOCAL.compressedSize, size],
          [ZIP_LOCAL.originalSize, originalSize],
        ])
          if (
            read32(local + field) !== expected &&
            (!(flags & ZIP_FLAGS.descriptor) || read32(local + field) !== 0)
          )
            throw new Error("Inconsistent ZIP size");
        selectedBytes += originalSize;
        if (selectedBytes > MAX_SELECTED_BYTES) throw new Error("3MF byte limit");
        if (ranges.has(name)) throw new Error("Duplicate ZIP entry");
        ranges.set(name, { start, size, originalSize, compression });
      }
      if (offset !== end) throw new Error("Invalid ZIP directory length");
      const entries: Record<string, Uint8Array> = {};
      for (const [name, declared] of ranges) {
        const entry = new Uint8Array(declared.originalSize);
        let length = 0;
        const receive = (data: Uint8Array) => {
          length += data.byteLength;
          extractedBytes += data.byteLength;
          if (length > declared.originalSize || extractedBytes > MAX_SELECTED_BYTES)
            throw new Error("3MF extracted byte limit");
          entry.set(data, length - data.byteLength);
        };
        const compressed = bytes.subarray(declared.start, declared.start + declared.size);
        if (declared.compression === ZIP_STORED) receive(compressed);
        else {
          const inflater = new Inflate(receive);
          for (
            let compressedOffset = 0;
            compressedOffset < compressed.length;
            compressedOffset += INFLATE_CHUNK_BYTES
          )
            inflater.push(
              compressed.subarray(compressedOffset, compressedOffset + INFLATE_CHUNK_BYTES),
              compressedOffset + INFLATE_CHUNK_BYTES >= compressed.length,
            );
        }
        if (length !== declared.originalSize) throw new Error("Inconsistent ZIP size");
        entries[name] = entry;
      }
      return entries;
    };
    const configs = extract(
      (name) =>
        name === "Metadata/model_settings.config" || name === "Metadata/project_settings.config",
    );
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

    const entries = new Map(
      Object.entries(extract((name) => name.endsWith(".model") || name === "_rels/.rels")),
    );
    const rootRelationship = Array.from(
      xml(entries.get("_rels/.rels")!).getElementsByTagName("Relationship"),
    ).find((relationship) => relationship.getAttribute("Type") === MODEL_RELATIONSHIP);
    if (!rootRelationship) return buffer;
    const rootPath = zipPath(rootRelationship.getAttribute("Target") ?? "");
    const models = new Map<string, Document>();
    const objects = new Map<string, Element>();
    // Moved objects acquire new IDs; keep their original component targets.
    const links = new Map<string, { id: string; path: string }[]>();
    let objectCount = 0;
    let componentCount = 0;
    for (const [path, data] of entries) {
      if (!path.endsWith(".model")) continue;
      const document = xml(data);
      entries.delete(path);
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
        links.set(
          key,
          children(object, "components").flatMap((components) =>
            children(components, "component").map((child) => ({
              id: child.getAttribute("objectid") ?? "",
              path: child.getAttributeNS(PRODUCTION, "path")
                ? zipPath(child.getAttributeNS(PRODUCTION, "path")!, normalizedPath)
                : normalizedPath,
            })),
          ),
        );
      }
    }
    const root = models.get(rootPath);
    if (!root) return buffer;
    const output = document.implementation.createDocument(CORE, "model");
    const outputModel = output.documentElement;
    const namespaces = new Map<string, string | null>();
    for (const model of models.values()) {
      for (const attribute of model.documentElement.attributes) {
        if (attribute.namespaceURI !== XMLNS || attribute.name === "xmlns") continue;
        const previous = namespaces.get(attribute.name);
        namespaces.set(
          attribute.name,
          previous === undefined || previous === attribute.value ? attribute.value : null,
        );
      }
    }
    for (const [name, value] of namespaces) {
      if (value !== null) outputModel.setAttributeNS(XMLNS, name, value);
    }
    if (root.documentElement.hasAttribute("unit"))
      outputModel.setAttribute("unit", root.documentElement.getAttribute("unit")!);
    const resources = output.createElementNS(CORE, "resources");
    const bases = output.createElementNS(CORE, "basematerials");
    bases.setAttribute("id", "1");
    resources.append(bases);
    outputModel.append(resources);
    const colors: string[] = [];
    const memo = new Map<string, { id: string; height: number }>();
    const sourceSizes = new Map<string, number>();
    const moved = new Set<string>();
    const emittedLimit = selectedBytes * MAX_EMITTED_RATIO + MAX_EMITTED_OVERHEAD;
    let emittedBytes = utf8Length(boundedSerialization(outputModel, emittedLimit));
    const reserve = (source: Element, key: string) => {
      let size = sourceSizes.get(key);
      if (size === undefined) {
        size = utf8Length(boundedSerialization(source, emittedLimit - emittedBytes));
        sourceSizes.set(key, size);
      }
      emittedBytes += size;
      if (emittedBytes > selectedBytes * MAX_EMITTED_RATIO + MAX_EMITTED_OVERHEAD)
        throw new Error("3MF emitted byte limit");
    };
    const visiting = new Set<string>();
    let nextId = 2;
    const clone = (
      path: string,
      id: string,
      parts: Map<string | null, Element>,
      inherited: number | null,
      assigned: Element | undefined,
      enabled: boolean,
      depth: number,
    ): { id: string; height: number } => {
      if (depth > MAX_COMPONENT_DEPTH) throw new Error("3MF component depth limit");
      const sourceKey = JSON.stringify([path, id]);
      const source = objects.get(sourceKey);
      if (!source || visiting.has(sourceKey)) throw new Error("Invalid component graph");
      const effectiveSlot = slot(assigned, inherited);
      const recolor = enabled && (!assigned || normal(assigned));
      const mesh = children(source, "mesh")[0];
      const effectiveColor = mesh && assigned && recolor ? color(effectiveSlot) : null;
      const key = JSON.stringify(
        mesh
          ? [sourceKey, effectiveColor]
          : [sourceKey, effectiveSlot, recolor, Boolean(assigned), depth === 0],
      );
      const previous = memo.get(key);
      if (previous) {
        if (depth + previous.height > MAX_COMPONENT_DEPTH)
          throw new Error("3MF component depth limit");
        return previous;
      }
      if (memo.size >= MAX_OBJECTS) throw new Error("3MF emitted object limit");
      reserve(source, sourceKey);
      visiting.add(sourceKey);
      const object = moved.has(sourceKey)
        ? output.importNode(source, true)
        : output.adoptNode(source);
      moved.add(sourceKey);
      let height = 0;
      let childIndex = 0;
      for (const components of children(object, "components")) {
        for (const child of children(components, "component")) {
          const target = links.get(sourceKey)![childIndex++];
          const cloned = clone(
            target.path,
            target.id,
            parts,
            effectiveSlot,
            depth === 0 ? (parts.get(target.id) ?? assigned) : assigned,
            recolor,
            depth + 1,
          );
          height = Math.max(height, cloned.height + 1);
          child.setAttribute("objectid", cloned.id);
          child.removeAttributeNS(PRODUCTION, "path");
        }
      }
      visiting.delete(sourceKey);
      if (memo.size >= MAX_OBJECTS) throw new Error("3MF emitted object limit");
      const result = { id: String(nextId++), height };
      memo.set(key, result);
      object.setAttribute("id", result.id);
      object.removeAttribute("pid");
      object.removeAttribute("pindex");
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
      return result;
    };
    const sourceBuild = children(root.documentElement, "build")[0];
    if (!sourceBuild) return buffer;
    reserve(sourceBuild, "build");
    const build = output.adoptNode(sourceBuild);
    for (const item of children(build, "item")) {
      const id = item.getAttribute("objectid") ?? "";
      const targetPath = item.getAttributeNS(PRODUCTION, "path");
      const path = targetPath ? zipPath(targetPath, rootPath) : rootPath;
      const source = objects.get(JSON.stringify([path, id]));
      if (!source) return buffer;
      const config = objectConfigs.get(id);
      const parts = new Map(
        config ? children(config, "part").map((part) => [part.getAttribute("id"), part]) : [],
      );
      item.setAttribute(
        "objectid",
        clone(
          path,
          id,
          parts,
          slot(config, 1),
          children(source, "mesh").length ? parts.get(id) : undefined,
          true,
          0,
        ).id,
      );
      item.removeAttributeNS(PRODUCTION, "path");
    }
    if (colors.length < 2) return buffer;
    outputModel.append(build);
    for (const model of models.values()) model.documentElement.replaceChildren();
    entries.clear();
    models.clear();
    objects.clear();
    links.clear();
    objectConfigs.clear();
    sourceSizes.clear();
    memo.clear();
    moved.clear();
    let serialized = new XMLSerializer().serializeToString(output);
    resources.replaceChildren();
    outputModel.replaceChildren();
    const encoded = strToU8(serialized);
    serialized = "";
    const modelBytes = new Uint8Array(encoded.buffer, encoded.byteOffset, encoded.byteLength);
    if (modelBytes.byteLength > selectedBytes * MAX_EMITTED_RATIO + MAX_EMITTED_OVERHEAD)
      return buffer;
    const relationships = `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="r" Type="${MODEL_RELATIONSHIP}"/></Relationships>`;
    return zipSync(
      {
        "_rels/.rels": new Uint8Array(strToU8(relationships)),
        "3D/3dmodel.model": modelBytes,
      },
      { level: 0 },
    ).buffer as ArrayBuffer;
  } catch {
    return buffer;
  }
}
