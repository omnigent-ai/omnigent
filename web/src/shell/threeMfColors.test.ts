import { describe, expect, it, vi } from "vitest";
import * as THREE from "three";
import { ThreeMFLoader } from "three/examples/jsm/loaders/3MFLoader.js";
import {
  strToU8,
  strFromU8,
  unzipSync,
  zipSync,
  Inflate,
  Zip,
  ZipDeflate,
  ZipPassThrough,
} from "three/examples/jsm/libs/fflate.module.js";
import {
  applyThreeMfColors,
  INFLATE_CHUNK_BYTES,
  MAX_SELECTED_BYTES,
  MAX_CONFIG_BYTES,
  MAX_XML_MARKUP,
  MAX_CONFIG_XML_MARKUP,
  MAX_EMITTED_ELEMENTS,
  MAX_EMITTED_ELEMENT_OVERHEAD,
  MAX_EMITTED_RATIO,
  MAX_EMITTED_OVERHEAD,
  MAX_EMITTED_BYTES,
} from "./threeMfColors";

const core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02";
const production = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06";
const palette = ["#F53B9D", "#4dc5a080", "#212329", "#FF7A18", "#FEFEFE"];
const rels =
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/root.model" Id="r" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>';
const transform = (x: number, y = 0, z = 0) => `1 0 0 0 1 0 0 0 1 ${x} ${y} ${z}`;
const model = (resources: string, build = '<item objectid="100"/>') =>
  `<model xmlns="${core}" xmlns:p="${production}" unit="millimeter"><resources>${resources}</resources><build>${build}</build></model>`;
const mesh = (id: number, width = 1, attributes = "") =>
  `<object id="${id}" type="model" ${attributes}><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="${width}" y="0" z="0"/><vertex x="0" y="2" z="0"/><vertex x="0" y="0" z="3"/></vertices><triangles><triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/><triangle v1="0" v2="3" v3="2"/><triangle v1="1" v2="2" v3="3"/></triangles></mesh></object>`;
const metadata = (value: string | number) => `<metadata key="extruder" value="${value}"/>`;
const part = (id: number, slot?: string | number, subtype = "normal_part") =>
  `<part id="${id}" subtype="${subtype}">${slot === undefined ? "" : metadata(slot)}</part>`;
const config = (parts: string, slot?: string | number, id = 100) =>
  `<object id="${id}">${slot === undefined ? "" : metadata(slot)}${parts}</object>`;
const composite = (id: number, components: string) =>
  `<object id="${id}" type="model"><components>${components}</components></object>`;
const component = (id: number, x = 0, path = "") =>
  `<component objectid="${id}" transform="${transform(x)}" ${path ? `p:path="${path}"` : ""}/>`;

function archive(
  overrides: Record<string, string | null> = {},
  reverse = false,
  level: 0 | 6 = 6,
): ArrayBuffer {
  const entries: Record<string, string | null> = {
    "_rels/.rels": rels,
    "3D/root.model": model(mesh(1) + mesh(2, 2) + composite(100, component(1) + component(2, 10))),
    "Metadata/model_settings.config": `<config>${config(part(1, 1) + part(2, 2))}</config>`,
    "Metadata/project_settings.config": JSON.stringify({ filament_colour: palette }),
    ...overrides,
  };
  const pairs = Object.entries(entries).filter(
    (entry): entry is [string, string] => entry[1] !== null,
  );
  if (reverse) pairs.reverse();
  const zipped = zipSync(
    Object.fromEntries(pairs.map(([path, text]) => [path, new Uint8Array(strToU8(text))])),
    { level },
  );
  return zipped.buffer as ArrayBuffer;
}

function descriptorArchive(pairs: [string, Uint8Array][], method: 0 | 8) {
  const chunks: Uint8Array[] = [];
  const zip = new Zip((error, chunk) => {
    if (error) throw error;
    chunks.push(chunk);
  });
  for (const [name, bytes] of pairs) {
    const entry = method === 8 ? new ZipDeflate(name) : new ZipPassThrough(name);
    zip.add(entry);
    entry.push(bytes, true);
  }
  zip.end();
  const bytes = new Uint8Array(chunks.reduce((total, chunk) => total + chunk.length, 0));
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return bytes.buffer;
}

function declareOriginalSize(input: ArrayBuffer, name: string, size: number) {
  const bytes = new Uint8Array(input);
  const view = new DataView(input);
  let offset = view.getUint32(input.byteLength - 6, true);
  const count = view.getUint16(input.byteLength - 12, true);
  for (let index = 0; index < count; index++) {
    const length = view.getUint16(offset + 28, true);
    if (strFromU8(bytes.subarray(offset + 46, offset + 46 + length)) === name) {
      view.setUint32(offset + 24, size, true);
      view.setUint32(view.getUint32(offset + 42, true) + 22, size, true);
      const local = view.getUint32(offset + 42, true);
      const start =
        local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
      return bytes.subarray(start, start + view.getUint32(offset + 20, true));
    }
    offset += 46 + length + view.getUint16(offset + 30, true) + view.getUint16(offset + 32, true);
  }
  throw new Error(`Missing fixture entry: ${name}`);
}

function rendered(buffer: ArrayBuffer) {
  const group = new ThreeMFLoader().parse(buffer);
  group.updateMatrixWorld(true);
  const rows: { hex: string; min: number[]; max: number[]; geometry: THREE.BufferGeometry }[] = [];
  group.traverse((object) => {
    if (object instanceof THREE.Mesh) {
      const material = object.material as THREE.MeshPhongMaterial;
      const bounds = new THREE.Box3().setFromObject(object);
      rows.push({
        hex: material.color.getHexString(THREE.SRGBColorSpace),
        min: bounds.min.toArray(),
        max: bounds.max.toArray(),
        geometry: object.geometry,
      });
    }
  });
  return rows;
}

function check(
  buffer: ArrayBuffer,
  colors = ["f53b9d", "4dc5a0"],
  positions = [0, 10],
  widths = [1, 2],
) {
  const rows = rendered(applyThreeMfColors(buffer));
  expect(rows.map((row) => row.hex)).toEqual(colors);
  expect(rows.map((row) => row.min)).toEqual(positions.map((x) => [x, 0, 0]));
  expect(rows.map((row) => row.max)).toEqual(
    positions.map((x, index) => [x + widths[index], 2, 3]),
  );
  return rows;
}

function multibyteVariants(note: string, count = 6) {
  return archive({
    "3D/root.model": model(
      mesh(1).replace("<mesh>", `<metadata name="note">${note}</metadata><mesh>`) +
        Array.from({ length: count }, (_, index) => composite(100 + index, component(1))).join(""),
      Array.from(
        { length: count },
        (_, index) => `<item objectid="${100 + index}" transform="${transform(index * 10)}"/>`,
      ).join(""),
    ),
    "Metadata/model_settings.config": `<config>${Array.from({ length: count }, (_, index) => config(part(1, index + 1), undefined, 100 + index)).join("")}</config>`,
    "Metadata/project_settings.config": JSON.stringify({
      filament_colour: ["#112233", "#223344", "#334455", "#445566", "#556677", "#667788"],
    }),
  });
}

describe("Bambu filament colours through the stock 3MF loader", () => {
  it.each(["Metadata/model_settings.config", "Metadata/project_settings.config"])(
    "bounds config bytes before inflation and parsing: %s",
    (name) => {
      const realistic = {
        "Metadata/model_settings.config": `<config>${config(part(1, 1) + part(2, 2) + Array.from({ length: 128 }, (_, index) => part(1000 + index, 1)).join(""))}</config>`,
        "Metadata/project_settings.config": JSON.stringify({
          filament_colour: palette,
          ...Object.fromEntries(
            Array.from({ length: 500 }, (_, index) => [`option_${index}`, "x".repeat(100)]),
          ),
        }),
      };
      const admitted = archive(realistic);
      check(admitted);
      const node = `<x note="${"x".repeat(500)}"/>`;
      const oversized = archive({
        ...realistic,
        [name]:
          name === "Metadata/model_settings.config"
            ? realistic[name].replace(
                "<config>",
                `<config><ignored>${node.repeat(Math.ceil(MAX_CONFIG_BYTES / node.length))}</ignored>`,
              )
            : JSON.stringify({ filament_colour: palette, ignored: "x".repeat(MAX_CONFIG_BYTES) }),
      });
      const forged = admitted.slice(0);
      declareOriginalSize(forged, name, MAX_CONFIG_BYTES + 1);
      const parser = vi.spyOn(DOMParser.prototype, "parseFromString");
      const inflater = vi.spyOn(Inflate.prototype, "push");
      try {
        for (const input of [oversized, forged]) {
          expect(applyThreeMfColors(input) === input).toBe(true);
          expect(parser).not.toHaveBeenCalled();
          expect(inflater).not.toHaveBeenCalled();
        }
      } finally {
        parser.mockRestore();
        inflater.mockRestore();
      }
      check(oversized, ["ffffff", "ffffff"]);
    },
  );

  it.each(["3D/root.model", "_rels/.rels"])(
    "bounds cumulative XML markup before any parsing: %s",
    (name) => {
      const entries = unzipSync(new Uint8Array(archive()));
      const markup = Object.entries(entries)
        .filter(([path]) => path !== "Metadata/project_settings.config")
        .reduce((count, [, bytes]) => count + strFromU8(bytes).split("<").length - 1, 0);
      // Inert annotations exercise the real byte preflight without allocating dense test DOMs.
      const fixture = (count: number) =>
        archive({ [name]: `<!--${"<".repeat(count)}-->${strFromU8(entries[name])}` });
      check(fixture(MAX_XML_MARKUP - markup - 1));
      const input = fixture(MAX_XML_MARKUP - markup);
      const parser = vi.spyOn(DOMParser.prototype, "parseFromString");
      try {
        expect(applyThreeMfColors(input) === input).toBe(true);
        expect(parser).not.toHaveBeenCalled();
      } finally {
        parser.mockRestore();
      }
    },
  );

  it("bounds dense config nodes before parsing with realistic plate headroom", () => {
    const entries = unzipSync(new Uint8Array(archive()));
    const settings = strFromU8(entries["Metadata/model_settings.config"]);
    const markers = settings.split("<").length - 1;
    check(
      archive({
        "Metadata/model_settings.config": `<!--${"<".repeat(MAX_CONFIG_XML_MARKUP - markers - 1)}-->${settings}`,
      }),
    );
    const input = archive({
      "Metadata/model_settings.config": settings.replace(
        "<config>",
        `<config><ignored>${"<x/>".repeat(MAX_CONFIG_XML_MARKUP)}</ignored>`,
      ),
    });
    const parser = vi.spyOn(DOMParser.prototype, "parseFromString");
    try {
      expect(applyThreeMfColors(input) === input).toBe(true);
      expect(parser).not.toHaveBeenCalled();
    } finally {
      parser.mockRestore();
    }
  });

  it("bounds dense shared-mesh copies before the excessive deep import", () => {
    const count = MAX_EMITTED_ELEMENT_OVERHEAD + 128;
    const fixture = (variants: number) =>
      archive({
        "3D/root.model": model(
          mesh(1).replace(
            "<mesh>",
            `<metadata name="note">${"<x/>".repeat(count)}</metadata><mesh>`,
          ) +
            Array.from({ length: variants }, (_, index) =>
              composite(100 + index, component(1)),
            ).join(""),
          Array.from(
            { length: variants },
            (_, index) => `<item objectid="${100 + index}" transform="${transform(index * 10)}"/>`,
          ).join(""),
        ),
        "Metadata/model_settings.config": `<config>${Array.from({ length: variants }, (_, index) => config(part(1, index + 1), undefined, 100 + index)).join("")}</config>`,
        "Metadata/project_settings.config": JSON.stringify({
          filament_colour: ["#112233", "#223344", "#334455", "#445566", "#556677", "#667788"],
        }),
      });
    check(fixture(3), ["112233", "223344", "334455"], [0, 10, 20], [1, 1, 1]);
    const input = fixture(6);
    const markup = Object.entries(unzipSync(new Uint8Array(input)))
      .filter(([path]) => path !== "Metadata/project_settings.config")
      .reduce((total, [, bytes]) => total + strFromU8(bytes).split("<").length - 1, 0);
    const limit = Math.min(
      markup * MAX_EMITTED_RATIO + MAX_EMITTED_ELEMENT_OVERHEAD,
      MAX_EMITTED_ELEMENTS,
    );
    let copiedElements = count;
    const original = Document.prototype.importNode;
    const importer = vi.spyOn(Document.prototype, "importNode").mockImplementation(function (
      this: Document,
      node,
      deep,
    ) {
      if (deep && node instanceof Element) copiedElements += node.querySelectorAll("*").length + 1;
      return original.call(this, node, deep);
    });
    const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
    try {
      expect(applyThreeMfColors(input) === input).toBe(true);
      expect(copiedElements).toBeLessThanOrEqual(limit);
      expect(serializer).not.toHaveBeenCalled();
    } finally {
      importer.mockRestore();
      serializer.mockRestore();
    }
  });

  it("counts neutral shared-mesh variants alongside emitted colours", () => {
    const shared = mesh(1).replace('x="0"', `x="0.${"0".repeat(1200 * 1024)}"`);
    const input = archive({
      "3D/root.model": model(
        [100, 101, 102]
          .map((id, index) => composite(id, component(1, index * 10, "/3D/shared.model")))
          .join(""),
        [100, 101, 102].map((id) => `<item objectid="${id}"/>`).join(""),
      ),
      "3D/shared.model": model(shared, ""),
      "Metadata/model_settings.config": `<config>${[1, 2, 9].map((slot, index) => config(part(1, slot), undefined, 100 + index)).join("")}</config>`,
    });
    expect(applyThreeMfColors(input)).not.toBe(input);
    check(input, ["f53b9d", "4dc5a0", "ffffff"], [0, 10, 20], [1, 1, 1]);
  });

  it("bounds two-colour composite variants by their actual palette size", () => {
    const fixture = (length: number) =>
      archive({
        "3D/root.model": model(
          mesh(1) +
            composite(300, component(1)).replace(
              "<components>",
              `<metadata name="note">${"x".repeat(length)}</metadata><components>`,
            ) +
            [100, 101, 102].map((id, index) => composite(id, component(300, index * 10))).join(""),
          [100, 101, 102].map((id) => `<item objectid="${id}"/>`).join(""),
        ),
        "Metadata/model_settings.config": `<config>${[100, 101, 102].map((id, index) => config(part(300, index + 1), undefined, id)).join("")}</config>`,
        "Metadata/project_settings.config": JSON.stringify({
          filament_colour: ["#F53B9D", "#4DC5A0", "#F53B9D"],
        }),
      });
    check(fixture(64), ["f53b9d", "4dc5a0", "f53b9d"], [0, 10, 20], [1, 1, 1]);
    const payload = MAX_EMITTED_OVERHEAD + 4096;
    const input = fixture(payload);
    const selected = Object.values(unzipSync(new Uint8Array(input))).reduce(
      (total, bytes) => total + bytes.length,
      0,
    );
    expect(payload * 3).toBeGreaterThan(selected * 2 + MAX_EMITTED_OVERHEAD);
    expect(payload * 3).toBeLessThan(selected * MAX_EMITTED_RATIO + MAX_EMITTED_OVERHEAD);
    const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
    try {
      expect(applyThreeMfColors(input) === input).toBe(true);
      expect(serializer).not.toHaveBeenCalled();
    } finally {
      serializer.mockRestore();
    }
  });

  it("keeps the two-colour budget independent of unrelated neutral build order", () => {
    const fixture = (length: number, neutralFirst: boolean) =>
      archive({
        "3D/root.model": model(
          mesh(1) +
            mesh(2, 2) +
            composite(300, component(1)).replace(
              "<components>",
              `<metadata name="note">${"x".repeat(length)}</metadata><components>`,
            ) +
            [100, 101, 102].map((id, index) => composite(id, component(300, index * 10))).join("") +
            composite(103, component(2, 40)),
          (neutralFirst ? [103, 100, 101, 102] : [100, 101, 102, 103])
            .map((id) => `<item objectid="${id}"/>`)
            .join(""),
        ),
        "Metadata/model_settings.config": `<config>${[100, 101, 102].map((id, index) => config(part(300, index + 1), undefined, id)).join("")}${config(part(2, 9), undefined, 103)}</config>`,
        "Metadata/project_settings.config": JSON.stringify({
          filament_colour: ["#F53B9D", "#4DC5A0", "#F53B9D"],
        }),
      });
    [true, false].forEach((first) => {
      check(
        fixture(64, first),
        first ? ["ffffff", "f53b9d", "4dc5a0", "f53b9d"] : ["f53b9d", "4dc5a0", "f53b9d", "ffffff"],
        first ? [40, 0, 10, 20] : [0, 10, 20, 40],
        first ? [2, 1, 1, 1] : [1, 1, 1, 2],
      );
      const input = fixture(MAX_EMITTED_OVERHEAD + 4096, first);
      const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
      try {
        expect(applyThreeMfColors(input) === input).toBe(true);
        expect(serializer).not.toHaveBeenCalled();
      } finally {
        serializer.mockRestore();
      }
    });
  });

  it("uses the central-directory palette instead of a stale local record", () => {
    const paletteName = "Metadata/project_settings.config";
    const pairs = Object.entries(unzipSync(new Uint8Array(archive())));
    const stale = strToU8(
      strFromU8(pairs.find(([name]) => name === paletteName)![1])
        .replace("#F53B9D", "#112233")
        .replace("#4dc5a080", "#44556680"),
    );
    const full = new Uint8Array(
      descriptorArchive(
        [
          [paletteName, stale],
          pairs[2],
          ["Metadata/padding.bin", new Uint8Array(2048)],
          ...pairs.filter(([name]) => name !== pairs[2][0]),
        ],
        0,
      ),
    );
    const view = new DataView(full.buffer);
    const directory = view.getUint32(full.length - 6, true);
    const removed =
      46 +
      view.getUint16(directory + 28, true) +
      view.getUint16(directory + 30, true) +
      view.getUint16(directory + 32, true);
    const bytes = new Uint8Array(full.length - removed);
    bytes.set(full.subarray(0, directory));
    bytes.set(full.subarray(directory + removed), directory);
    const end = bytes.length - 22;
    const edited = new DataView(bytes.buffer);
    edited.setUint16(end + 8, pairs.length + 1, true);
    edited.setUint16(end + 10, pairs.length + 1, true);
    edited.setUint32(end + 12, edited.getUint32(end + 12, true) - removed, true);
    expect(strFromU8(unzipSync(bytes)[paletteName])).toContain("#F53B9D");
    expect(applyThreeMfColors(bytes.buffer)).not.toBe(bytes.buffer);
    check(bytes.buffer);
  });

  it("ignores local-header signatures inside unselected descriptor payloads", () => {
    const payload = new Uint8Array(80);
    const view = new DataView(payload.buffer);
    view.setUint32(0, 0x04034b50, true);
    view.setUint16(26, 4, true);
    payload.set(strToU8("junk"), 30);
    view.setUint32(18, 999999, true);
    const pairs = Object.entries(unzipSync(new Uint8Array(archive())));
    for (const method of [0, 8] as const) {
      for (const reverse of [false, true]) {
        const input = descriptorArchive(
          [["Metadata/thumbnail.png", payload], ...(reverse ? [...pairs].reverse() : pairs)],
          method,
        );
        expect(unzipSync(new Uint8Array(input))["Metadata/thumbnail.png"]).toEqual(payload);
        expect(applyThreeMfColors(input)).not.toBe(input);
        check(input);
        const invalid = input.slice(0);
        const edited = new DataView(invalid);
        let directory = edited.getUint32(invalid.byteLength - 6, true);
        while (
          strFromU8(
            new Uint8Array(invalid, directory + 46, edited.getUint16(directory + 28, true)),
          ) !== "Metadata/model_settings.config"
        )
          directory +=
            46 +
            edited.getUint16(directory + 28, true) +
            edited.getUint16(directory + 30, true) +
            edited.getUint16(directory + 32, true);
        edited.setUint32(directory + 20, 1, true);
        expect(applyThreeMfColors(invalid)).toBe(invalid);
        if (method === 0) check(invalid, ["ffffff", "ffffff"]);
        // The stock loader cannot unzip the truncated DEFLATE range.
        else expect(() => rendered(invalid)).toThrow();
      }
    }
  });

  it("aborts incremental inflate after a forged valid XML prefix", () => {
    const control = archive();
    check(control);
    const name = "Metadata/model_settings.config";
    const prefix = strFromU8(unzipSync(new Uint8Array(control))[name]);
    let state = 123456789;
    const tail = Array.from({ length: 4 * INFLATE_CHUNK_BYTES }, () => {
      state ^= state << 13;
      state ^= state >>> 17;
      state ^= state << 5;
      return String.fromCharCode(32 + ((state >>> 0) % 95));
    }).join("");
    const input = archive({ [name]: prefix + " ".repeat(INFLATE_CHUNK_BYTES) + tail });
    expect(declareOriginalSize(input, name, prefix.length).byteLength).toBeGreaterThan(
      2 * INFLATE_CHUNK_BYTES,
    );
    expect(strFromU8(unzipSync(new Uint8Array(input))[name])).toBe(prefix);
    const parser = vi.spyOn(DOMParser.prototype, "parseFromString");
    const inflater = vi.spyOn(Inflate.prototype, "push");
    const copier = vi.spyOn(Uint8Array.prototype, "set");
    try {
      expect(applyThreeMfColors(input)).toBe(input);
      expect(parser).not.toHaveBeenCalled();
      const compressedBytes = inflater.mock.calls.reduce(
        (total, [chunk]) => total + chunk.length,
        0,
      );
      expect(compressedBytes).toBeGreaterThan(0);
      expect(compressedBytes).toBeLessThan(2 * INFLATE_CHUNK_BYTES);
      const overrunBytes = copier.mock.calls.reduce(
        (total, [chunk, offset], index) =>
          total +
          Math.max(
            0,
            chunk.length + (offset ?? 0) - (copier.mock.contexts[index] as Uint8Array).byteLength,
          ),
        0,
      );
      expect(overrunBytes).toBe(0);
    } finally {
      parser.mockRestore();
      inflater.mockRestore();
      copier.mockRestore();
    }
    check(input, ["ffffff", "ffffff"]);
  });

  it("rejects selected payloads above the large-model ceiling before inflation", () => {
    check(archive());
    const input = archive();
    declareOriginalSize(input, "Metadata/model_settings.config", 129 * 1024 * 1024);
    const inflater = vi.spyOn(Inflate.prototype, "push");
    const parser = vi.spyOn(DOMParser.prototype, "parseFromString");
    try {
      expect(applyThreeMfColors(input)).toBe(input);
      expect(inflater).not.toHaveBeenCalled();
      expect(parser).not.toHaveBeenCalled();
    } finally {
      inflater.mockRestore();
      parser.mockRestore();
    }
    check(input, ["ffffff", "ffffff"]);
  });

  it("rejects forged selected ZIP sizes before parsing oversized XML", () => {
    const control = archive();
    expect(applyThreeMfColors(control)).not.toBe(control);
    check(control);
    const forgeSize = (input: ArrayBuffer, name: string, size: number) => {
      const bytes = new Uint8Array(input);
      const view = new DataView(input);
      for (let offset = bytes.length - 22; offset >= 0; offset--) {
        if (view.getUint32(offset, true) !== 0x02014b50) continue;
        const filename = strFromU8(
          bytes.subarray(offset + 46, offset + 46 + view.getUint16(offset + 28, true)),
        );
        if (filename === name) view.setUint32(offset + 24, size, true);
      }
    };
    const parser = vi.spyOn(DOMParser.prototype, "parseFromString");
    const copier = vi.spyOn(Uint8Array.prototype, "set");
    try {
      for (const name of ["Metadata/model_settings.config", "3D/root.model"]) {
        const input = archive(
          name.endsWith(".config") ? { [name]: `<config>${" ".repeat(8 * 1024)}</config>` } : {},
          false,
          0,
        );
        forgeSize(input, name, 1);
        parser.mockClear();
        expect(applyThreeMfColors(input)).toBe(input);
        expect(
          parser.mock.calls.every(
            ([text]) => strToU8(String(text)).byteLength <= MAX_SELECTED_BYTES,
          ),
        ).toBe(true);
        check(input, ["ffffff", "ffffff"]);
      }
      const name = "Metadata/model_settings.config";
      const length = unzipSync(new Uint8Array(control))[name].byteLength;
      for (const size of [1, length + 1]) {
        const input = archive();
        forgeSize(input, name, size);
        const extracted = unzipSync(new Uint8Array(input), {
          filter: (entry) => {
            if (entry.name !== name) return false;
            expect(entry.compression).toBe(8);
            return true;
          },
        })[name];
        expect(extracted.byteLength).toBe(Math.min(size, length));
        expect(strFromU8(extracted)).toBe(
          strFromU8(unzipSync(new Uint8Array(control))[name]).slice(0, size),
        );
        parser.mockClear();
        expect(applyThreeMfColors(input)).toBe(input);
        expect(parser).not.toHaveBeenCalled();
        check(input, ["ffffff", "ffffff"]);
      }
      const overrunBytes = copier.mock.calls.reduce(
        (total, [chunk, offset], index) =>
          total +
          Math.max(
            0,
            chunk.length + (offset ?? 0) - (copier.mock.contexts[index] as Uint8Array).byteLength,
          ),
        0,
      );
      expect(overrunBytes).toBe(0);
    } finally {
      parser.mockRestore();
      copier.mockRestore();
    }
  });

  it("enforces cached subtree depth independently of build order", () => {
    const chain = (start: number, length: number, target: number) =>
      Array.from({ length }, (_, index) =>
        composite(start + index, component(index === length - 1 ? target : start + index + 1)),
      ).join("");
    for (const reverse of [false, true]) {
      const fixture = (inner: number, outer: number) => {
        const items = [
          `<item objectid="200" transform="${transform(0)}"/>`,
          `<item objectid="300" transform="${transform(20)}"/>`,
        ];
        if (reverse) items.reverse();
        return archive({
          "3D/root.model": model(
            mesh(1) +
              mesh(2, 2) +
              chain(10, inner, 1) +
              chain(100, outer, 10) +
              composite(200, component(10) + component(2, 10)) +
              composite(300, component(100)),
            items.join(""),
          ),
          "Metadata/model_settings.config": `<config>${config(part(10, 1) + part(2, 2), undefined, 200)}${config(part(100, 1), undefined, 300)}</config>`,
        });
      };
      const positions = reverse ? [20, 0, 10] : [0, 10, 20];
      const widths = reverse ? [1, 1, 2] : [1, 2, 1];
      check(
        fixture(31, 32),
        reverse ? ["f53b9d", "f53b9d", "4dc5a0"] : ["f53b9d", "4dc5a0", "f53b9d"],
        positions,
        widths,
      );
      const input = fixture(40, 40);
      expect(applyThreeMfColors(input)).toBe(input);
      check(input, ["ffffff", "ffffff", "ffffff"], positions, widths);
    }
  });

  it("rejects selected XML doctypes before entity expansion", () => {
    const control = archive();
    check(control);
    const settings = strFromU8(
      unzipSync(new Uint8Array(control))["Metadata/model_settings.config"],
    );
    const parser = vi.spyOn(DOMParser.prototype, "parseFromString");
    try {
      for (const declaration of [
        '<!ENTITY text "lol"><!ENTITY repeated "&text;&text;&text;&text;&text;&text;&text;&text;&text;&text;">',
        '<!ENTITY repeated SYSTEM "http://127.0.0.1/external-entity">',
      ]) {
        const input = archive({
          "Metadata/model_settings.config": `<!DOCTYPE config [${declaration}]>${settings.replace("<config>", "<config>&repeated;")}`,
        });
        parser.mockClear();
        expect(applyThreeMfColors(input)).toBe(input);
        expect(parser).not.toHaveBeenCalled();
        check(input, ["ffffff", "ffffff"]);
      }
      for (const [name, root] of [
        ["3D/root.model", "model"],
        ["_rels/.rels", "Relationships"],
      ]) {
        const source = strFromU8(unzipSync(new Uint8Array(control))[name]);
        const input = archive({ [name]: `<!DOCTYPE ${root}>${source}` });
        parser.mockClear();
        expect(applyThreeMfColors(input)).toBe(input);
        expect(parser.mock.calls.some(([text]) => String(text).includes("<!DOCTYPE"))).toBe(false);
        check(input, ["ffffff", "ffffff"]);
      }
    } finally {
      parser.mockRestore();
    }
  });

  it("normalizes harmless DOCTYPE text inside comments, instructions and CDATA", () => {
    const control = archive();
    const source = strFromU8(unzipSync(new Uint8Array(control))["3D/root.model"]);
    for (const text of [
      `<?xml version="1.0"?>\n<!-- <!DOCTYPE model> --><?note <!DOCTYPE?>${source}`,
      source.replace(
        "<resources>",
        '<metadata name="note"><![CDATA[<!DOCTYPE model>]]></metadata><resources>',
      ),
    ]) {
      const input = archive({ "3D/root.model": text });
      expect(applyThreeMfColors(input)).not.toBe(input);
      check(input);
    }
  });

  it("shares inherited namespaces instead of redeclaring them on each mesh", () => {
    const count = 80;
    const namespace = production;
    const input = archive({
      "3D/root.model": model(
        Array.from({ length: count }, (_, index) => mesh(index + 1, 1, 'x:note="a"')).join("") +
          composite(
            100,
            Array.from({ length: count }, (_, index) => component(index + 1, index * 2)).join(""),
          ),
      ).replace("<model ", `<model xmlns:x="${namespace}" `),
      "Metadata/model_settings.config": `<config>${config(Array.from({ length: count }, (_, index) => part(index + 1, (index % 2) + 1)).join(""))}</config>`,
    });
    const normalized = applyThreeMfColors(input);
    expect(normalized).not.toBe(input);
    const text = strFromU8(unzipSync(new Uint8Array(normalized))["3D/3dmodel.model"]);
    expect(text.match(/xmlns:x=/g)).toHaveLength(1);
    expect(
      new DOMParser()
        .parseFromString(text, "application/xml")
        .documentElement.getAttribute("xmlns:x"),
    ).toBe(namespace);
    check(
      input,
      Array.from({ length: count }, (_, index) => (index % 2 ? "4dc5a0" : "f53b9d")),
      Array.from({ length: count }, (_, index) => index * 2),
      Array.from({ length: count }, () => 1),
    );
  });

  it("bounds inherited namespace expansion on first mesh emissions", () => {
    const count = 80;
    const namespace = `https://example.test/${"n".repeat(16384)}`;
    const fixture = (uri: string) =>
      archive({
        "3D/root.model": model(
          Array.from({ length: count }, (_, index) => mesh(index + 1, 1, 'x:note="a"')).join("") +
            composite(
              100,
              Array.from({ length: count }, (_, index) => component(index + 1, index * 2)).join(""),
            ),
        ).replace("<model ", `<model xmlns:x="${uri}" `),
        "Metadata/model_settings.config": `<config>${config(Array.from({ length: count }, (_, index) => part(index + 1, (index % 2) + 1)).join(""))}</config>`,
      });
    check(
      fixture(production),
      Array.from({ length: count }, (_, index) => (index % 2 ? "4dc5a0" : "f53b9d")),
      Array.from({ length: count }, (_, index) => index * 2),
      Array(count).fill(1),
    );
    const input = fixture(namespace);
    const selected = Object.values(unzipSync(new Uint8Array(input))).reduce(
      (total, entry) => total + entry.length,
      0,
    );
    expect(selected).toBeLessThan(64 * 1024);
    const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
    try {
      expect(applyThreeMfColors(input) === input).toBe(true);
      const measuredBytes = serializer.mock.results.reduce(
        (total, result) => total + strToU8(String(result.value)).byteLength,
        0,
      );
      expect(measuredBytes).toBeLessThan(selected * 2 + 1024 * 1024 + 64 * 1024);
    } finally {
      serializer.mockRestore();
    }
    check(
      input,
      Array.from({ length: count }, () => "ffffff"),
      Array.from({ length: count }, (_, index) => index * 2),
      Array.from({ length: count }, () => 1),
    );
  });

  it("moves first mesh emissions and clones only copy-on-write variants", () => {
    const input = archive({
      "3D/root.model": model(
        mesh(1) + [100, 101, 102].map((id) => composite(id, component(1))).join(""),
        [100, 101, 102, 100]
          .map((id, index) => `<item objectid="${id}" transform="${transform(index * 10)}"/>`)
          .join(""),
      ),
      "Metadata/model_settings.config": `<config>${[1, 2, 99].map((slot, index) => config(part(1, slot), undefined, 100 + index)).join("")}</config>`,
    });
    const importer = vi.spyOn(Document.prototype, "importNode");
    try {
      const rows = check(
        input,
        ["f53b9d", "4dc5a0", "ffffff", "f53b9d"],
        [0, 10, 20, 30],
        [1, 1, 1, 1],
      );
      expect(rows[0].geometry).toBe(rows[3].geometry);
      const meshes = importer.mock.calls.filter(
        ([node]) => node instanceof Element && node.querySelector("mesh"),
      );
      expect(meshes.length).toBeLessThanOrEqual(2);
    } finally {
      importer.mockRestore();
    }
  });

  it("keeps original nested component links when moved IDs collide with source meshes", () => {
    const input = archive({
      "3D/root.model": model(
        mesh(1) +
          mesh(2, 5) +
          composite(10, component(1)) +
          composite(100, component(10)) +
          composite(200, component(10, 20)),
        '<item objectid="100"/><item objectid="200"/>',
      ),
      "Metadata/model_settings.config": `<config>${config(part(10, 1), undefined, 100)}${config(part(10, 2), undefined, 200)}</config>`,
    });
    check(input, ["f53b9d", "4dc5a0"], [0, 20], [1, 1]);
  });

  it("rejects prefix substitution before any oversized serialization", () => {
    check(archive());
    const prefix = "n".repeat(8192);
    const input = archive({
      "3D/root.model": model(
        mesh(1, 1, `xmlns:${prefix}="urn:x"`).replace(
          "</vertices>",
          `${'<vertex x="0" y="0" z="0" a:note="a"/>'.repeat(180)}</vertices>`,
        ) +
          mesh(2, 2) +
          composite(100, component(1) + component(2, 10)),
      ).replace("<model ", '<model xmlns:a="urn:x" '),
    });
    const selected = Object.values(unzipSync(new Uint8Array(input))).reduce(
      (total, entry) => total + entry.byteLength,
      0,
    );
    const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
    try {
      expect(applyThreeMfColors(input)).toBe(input);
      for (const result of serializer.mock.results)
        expect(strToU8(String(result.value)).byteLength).toBeLessThanOrEqual(
          selected * 2 + 1024 * 1024,
        );
    } finally {
      serializer.mockRestore();
    }
    check(input, ["ffffff", "ffffff"]);
  });

  it("admits slicer namespaces and passes unsupported namespace layouts through", () => {
    const slicer = "http://schemas.bambulab.com/package/2021";
    const admitted = archive({
      "3D/root.model": model(
        mesh(1, 1, 'p:UUID="one" BambuStudio:note="colour"') +
          mesh(2, 2) +
          composite(100, component(1) + component(2, 10)),
      ).replace("<model ", `<model xmlns:BambuStudio="${slicer}" `),
    });
    const admittedSerializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
    try {
      check(admitted);
      expect(admittedSerializer.mock.calls.length).toBe(1);
    } finally {
      admittedSerializer.mockRestore();
    }
    const standard = model(mesh(1) + mesh(2, 2) + composite(100, component(1) + component(2, 10)));
    const layouts: Record<string, string | null>[] = [
      { "3D/root.model": standard.replace("<model ", '<model xmlns:q="urn:unknown" ') },
      {
        "3D/root.model": standard.replace(
          "<model ",
          `<model xmlns:${"q".repeat(13)}="${production}" `,
        ),
      },
      {
        "3D/root.model": standard.replace(
          '<object id="1"',
          `<object xmlns:q="${production}" id="1"`,
        ),
      },
      {
        "3D/root.model": standard.replace("<model ", `<model xmlns:q="${production}" `),
        "3D/Objects/other.model": standard.replace("<model ", `<model xmlns:q="${slicer}" `),
      },
      { "3D/root.model": standard.replace(`xmlns="${core}"`, `xmlns="${production}"`) },
    ];
    const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
    try {
      for (const overrides of layouts) {
        const input = archive(overrides);
        serializer.mockClear();
        expect(applyThreeMfColors(input)).toBe(input);
        expect(serializer.mock.calls.length).toBe(0);
        check(input, ["ffffff", "ffffff"]);
      }
    } finally {
      serializer.mockRestore();
    }
  });

  it("resolves config parts with linear inherited-slot work", () => {
    const count = 512;
    const input = archive({
      "Metadata/model_settings.config": `<config>${config(Array.from({ length: count }, (_, index) => (index === 1 ? part(2, 2) : `<part id="${index + 1}"/>`)).join(""), 1)}</config>`,
    });
    const getter = Object.getOwnPropertyDescriptor(Element.prototype, "children")!.get!;
    let visited = 0;
    const childrenGetter = vi
      .spyOn(Element.prototype, "children", "get")
      .mockImplementation(function (this: Element) {
        const result = getter.call(this) as HTMLCollection;
        if (this.localName === "object" && this.parentElement?.localName === "config") {
          visited += result.length;
          if (visited > count * 8) throw new Error("Quadratic config rescanning");
        }
        return result;
      });
    try {
      check(input);
      expect(visited).toBeLessThan(count * 8);
    } finally {
      childrenGetter.mockRestore();
    }
  });

  it("indexes configuration once across repeated build items", () => {
    const count = 128;
    const input = archive({
      "3D/root.model": model(
        mesh(1) + mesh(2, 2) + composite(100, component(1) + component(2, 10)),
        Array.from(
          { length: count },
          (_, index) => `<item objectid="100" transform="${transform(index * 20)}"/>`,
        ).join(""),
      ),
      "Metadata/model_settings.config": `<config>${config(Array.from({ length: count }, (_, index) => part(index + 1, index === 1 ? 2 : 0)).join(""), 1)}</config>`,
    });
    const getter = Object.getOwnPropertyDescriptor(Element.prototype, "children")!.get!;
    let visits = 0;
    const observer = vi.spyOn(Element.prototype, "children", "get").mockImplementation(function (
      this: Element,
    ) {
      const result = getter.call(this) as HTMLCollection;
      if (this.localName === "object" && this.parentElement?.localName === "config") {
        visits += result.length;
        if (visits > count * 8) throw new Error("Repeated configuration scan");
      }
      return result;
    });
    try {
      check(
        input,
        Array.from({ length: count }, () => ["f53b9d", "4dc5a0"]).flat(),
        Array.from({ length: count }, (_, index) => [index * 20, index * 20 + 10]).flat(),
        Array.from({ length: count }, () => [1, 2]).flat(),
      );
      expect(visits).toBeLessThan(count * 8);
    } finally {
      observer.mockRestore();
    }
  });

  it("reads explicit part slots once across repeated component instances", () => {
    const count = 128;
    const input = archive({
      "3D/root.model": model(
        mesh(1) +
          mesh(2, 2) +
          composite(
            100,
            Array.from({ length: count }, (_, index) => component(1, index * 10)).join("") +
              component(2, count * 10),
          ),
      ),
      "Metadata/model_settings.config": `<config>${config(
        `<part id="1">${'<metadata key="note" value="x"/>'.repeat(count)}${metadata(0)}</part>` +
          part(2, 2) +
          Array.from({ length: count }, (_, index) => part(index + 3, 0)).join(""),
        1,
      )}</config>`,
    });
    const getter = Object.getOwnPropertyDescriptor(Element.prototype, "children")!.get!;
    let visits = 0;
    const observer = vi.spyOn(Element.prototype, "children", "get").mockImplementation(function (
      this: Element,
    ) {
      const result = getter.call(this) as HTMLCollection;
      if (this.localName === "part") {
        visits += result.length;
        if (visits > count * 8) throw new Error("Repeated part metadata scan");
      }
      return result;
    });
    try {
      check(
        input,
        [...Array(count).fill("f53b9d"), "4dc5a0"],
        Array.from({ length: count + 1 }, (_, index) => index * 10),
        [...Array(count).fill(1), 2],
      );
      expect(visits).toBeLessThan(count * 8);
    } finally {
      observer.mockRestore();
    }
  });

  it("normalizes root-bound aliases without per-descendant namespace charges", () => {
    const input = archive({
      "3D/root.model": model(
        mesh(1).replace(
          "<mesh>",
          `<metadata name="note">${'<p:n p:note="a"/>'.repeat(7000)}</metadata><mesh>`,
        ) +
          mesh(2, 2) +
          composite(100, component(1) + component(2, 10)),
      ).replace("<model ", `<model xmlns:abcdefghijkl="${production}" `),
    });
    check(input);
  });

  it.each([false, true])(
    "indexes source mesh presence for repeated components=%s",
    (components) => {
      const count = 128;
      const input = archive({
        "3D/root.model": model(
          mesh(1).replace("<mesh>", `${'<metadata name="bulk">x</metadata>'.repeat(count)}<mesh>`) +
            mesh(2, 2) +
            (components
              ? composite(
                  100,
                  Array.from({ length: count }, (_, index) => component(1, index * 10)).join("") +
                    component(2, count * 10),
                )
              : ""),
          components
            ? '<item objectid="100"/>'
            : Array.from(
                { length: count },
                (_, index) => `<item objectid="1" transform="${transform(index * 10)}"/>`,
              ).join("") + `<item objectid="2" transform="${transform(count * 10)}"/>`,
        ),
        "Metadata/model_settings.config": `<config>${
          components
            ? config(part(1, 1) + part(2, 2))
            : config(part(1, 1), undefined, 1) + config(part(2, 2), undefined, 2)
        }</config>`,
      });
      const getter = Object.getOwnPropertyDescriptor(Element.prototype, "children")!.get!;
      let visits = 0;
      const observer = vi.spyOn(Element.prototype, "children", "get").mockImplementation(function (
        this: Element,
      ) {
        const result = getter.call(this) as HTMLCollection;
        if (
          this.localName === "object" &&
          this.firstElementChild?.getAttribute("name") === "bulk"
        ) {
          visits += result.length;
          if (visits > count * 8) throw new Error("Repeated source mesh scan");
        }
        return result;
      });
      try {
        check(
          input,
          [...Array(count).fill("f53b9d"), "4dc5a0"],
          Array.from({ length: count + 1 }, (_, index) => index * 10),
          [...Array(count).fill(1), 2],
        );
        expect(visits).toBeLessThan(count * 8);
      } finally {
        observer.mockRestore();
      }
    },
  );

  it("recolours three ShareMesh copies larger than one MiB in different filaments", () => {
    const shared = mesh(1).replace('x="0"', `x="0.${"0".repeat(1200 * 1024)}"`);
    const input = archive({
      "3D/root.model": model(
        [100, 101, 102]
          .map((id, index) => composite(id, component(1, index * 10, "/3D/shared.model")))
          .join(""),
        [100, 101, 102].map((id) => `<item objectid="${id}"/>`).join(""),
      ),
      "3D/shared.model": model(shared, ""),
      "Metadata/model_settings.config": `<config>${[100, 101, 102].map((id, index) => config(part(1, index + 1), undefined, id)).join("")}</config>`,
    });
    const selected = Object.values(unzipSync(new Uint8Array(input))).reduce(
      (sum, bytes) => sum + bytes.length,
      0,
    );
    expect(selected).toBeGreaterThan(1024 * 1024);
    const normalized = applyThreeMfColors(input);
    expect(normalized).not.toBe(input);
    const outputBytes = unzipSync(new Uint8Array(normalized))["3D/3dmodel.model"].length;
    expect(outputBytes).toBeGreaterThan(selected * 2 + MAX_EMITTED_OVERHEAD);
    expect(outputBytes).toBeLessThan(selected * 3 + MAX_EMITTED_OVERHEAD);
    expect(outputBytes).toBeLessThan(MAX_EMITTED_BYTES);
    const rows = rendered(normalized);
    expect(rows.map((row) => row.hex)).toEqual(["f53b9d", "4dc5a0", "212329"]);
    expect(rows.map((row) => row.min)).toEqual([
      [0, 0, 0],
      [10, 0, 0],
      [20, 0, 0],
    ]);
    expect(rows.map((row) => row.max)).toEqual([
      [1, 2, 3],
      [11, 2, 3],
      [21, 2, 3],
    ]);
  });

  it("reserves generated appearance bytes before serializing a near-budget model", () => {
    const count = MAX_EMITTED_RATIO + 1;
    const admitted = multibyteVariants("x".repeat(64), count);
    check(
      admitted,
      ["112233", "223344", "334455", "445566", "556677"],
      [0, 10, 20, 30, 40],
      Array(count).fill(1),
    );
    const empty = multibyteVariants("", count);
    const selected = Object.values(unzipSync(new Uint8Array(empty))).reduce(
      (sum, bytes) => sum + bytes.length,
      0,
    );
    const outputSize = unzipSync(new Uint8Array(applyThreeMfColors(empty)))["3D/3dmodel.model"]
      .length;
    const length = selected * MAX_EMITTED_RATIO + MAX_EMITTED_OVERHEAD - outputSize + 1;
    const input = multibyteVariants("x".repeat(length), count);
    const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
    try {
      expect(applyThreeMfColors(input)).toBe(input);
      expect(serializer).not.toHaveBeenCalled();
    } finally {
      serializer.mockRestore();
    }
  });

  it("reserves rewritten ID growth before serializing near-budget references", () => {
    const fixture = (length: number, count: number, components: boolean) => {
      const dummyIds = Array.from({ length: 8 }, (_, index) => index + 20);
      const composites = [100, 101, 102, 103, 104];
      return archive({
        "3D/root.model": model(
          dummyIds.map((id) => mesh(id)).join("") +
            mesh(1).replace(
              "<mesh>",
              `<metadata name="note">${"x".repeat(length)}</metadata><mesh>`,
            ) +
            composites.map((id) => composite(id, component(1))).join("") +
            (components ? composite(300, component(1).repeat(count)) : ""),
          [...dummyIds, ...composites].map((id) => `<item objectid="${id}"/>`).join("") +
            (components ? '<item objectid="300"/>' : '<item objectid="1"/>'.repeat(count)),
        ),
        "Metadata/model_settings.config": `<config>${config(part(1, 1), undefined, 1)}${composites.map((id, index) => config(part(1, index + 1), undefined, id)).join("")}${components ? config(part(1, 1), undefined, 300) : ""}</config>`,
      });
    };
    [false, true].forEach((components) => {
      check(
        fixture(32, 8, components),
        [
          ...Array(8).fill("ffffff"),
          "f53b9d",
          "4dc5a0",
          "212329",
          "ff7a18",
          "fefefe",
          ...Array(8).fill("f53b9d"),
        ],
        Array(21).fill(0),
        Array(21).fill(1),
      );
      const base = fixture(0, 2400, components);
      const selected = Object.values(unzipSync(new Uint8Array(base))).reduce(
        (sum, bytes) => sum + bytes.length,
        0,
      );
      const normalized = applyThreeMfColors(base);
      expect(normalized).not.toBe(base);
      const outputSize = unzipSync(new Uint8Array(normalized))["3D/3dmodel.model"].length;
      const length = selected * MAX_EMITTED_RATIO + MAX_EMITTED_OVERHEAD - outputSize + 1;
      const input = fixture(length, 2400, components);
      const budget = (selected + length) * MAX_EMITTED_RATIO + MAX_EMITTED_OVERHEAD;
      const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
      try {
        expect(applyThreeMfColors(input)).toBe(input);
        for (const result of serializer.mock.results)
          expect(strToU8(String(result.value)).byteLength).toBeLessThanOrEqual(budget);
      } finally {
        serializer.mockRestore();
      }
    });
  });

  it("bounds inherited namespace expansion before standalone subtree serialization", () => {
    const namespace = `urn:${"x".repeat(8192)}`;
    const fixture = (count: number, uri = namespace) =>
      archive({
        "3D/root.model": model(
          mesh(1).replace(
            "</vertices>",
            `${'<vertex x="0" y="0" z="0" x:note="a"/>'.repeat(count)}</vertices>`,
          ) +
            mesh(2, 2) +
            composite(100, component(1) + component(2, 10)),
        ).replace("<model ", `<model xmlns:x="${uri}" `),
      });
    const admitted = fixture(2, production);
    expect(applyThreeMfColors(admitted)).not.toBe(admitted);
    check(admitted);
    const input = fixture(180);
    const selected = Object.values(unzipSync(new Uint8Array(input))).reduce(
      (total, entry) => total + entry.byteLength,
      0,
    );
    const budget = selected * 2 + 1024 * 1024;
    const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
    try {
      expect(applyThreeMfColors(input)).toBe(input);
      for (const result of serializer.mock.results) {
        expect(strToU8(String(result.value)).byteLength).toBeLessThanOrEqual(budget);
      }
    } finally {
      serializer.mockRestore();
    }
    check(input, ["ffffff", "ffffff"]);
  });

  it("bounds cumulative colour-variant expansion before cloning meshes", () => {
    const count = 35;
    const fixture = (payload: string) =>
      archive({
        "3D/root.model": model(
          mesh(1).replace("<mesh>", `<metadata name="large">${payload}</metadata><mesh>`) +
            Array.from({ length: count }, (_, index) => composite(100 + index, component(1))).join(
              "",
            ),
          Array.from(
            { length: count },
            (_, index) => `<item objectid="${100 + index}" transform="${transform(index * 10)}"/>`,
          ).join(""),
        ),
        "Metadata/model_settings.config": `<config>${Array.from({ length: count }, (_, index) => config(part(1, index + 1), undefined, 100 + index)).join("")}</config>`,
        "Metadata/project_settings.config": JSON.stringify({
          filament_colour: Array.from(
            { length: count },
            (_, index) => `#${(index + 1).toString(16).padStart(6, "0")}`,
          ),
        }),
      });
    const admitted = fixture("x".repeat(1024));
    expect(applyThreeMfColors(admitted)).not.toBe(admitted);
    check(
      admitted,
      Array.from({ length: count }, (_, index) => (index + 1).toString(16).padStart(6, "0")),
      Array.from({ length: count }, (_, index) => index * 10),
      Array.from({ length: count }, () => 1),
    );
    const payloadBytes = 64 * 1024;
    const input = fixture("x".repeat(payloadBytes));
    const selected = Object.values(unzipSync(new Uint8Array(input))).reduce(
      (sum, bytes) => sum + bytes.length,
      0,
    );
    const budget = Math.min(selected * MAX_EMITTED_RATIO + MAX_EMITTED_OVERHEAD, MAX_EMITTED_BYTES);
    const importer = vi.spyOn(Document.prototype, "importNode");
    try {
      expect(applyThreeMfColors(input) === input).toBe(true);
      const meshCopies = importer.mock.calls.filter(
        ([node]) => node instanceof Element && node.querySelector("mesh"),
      );
      expect(meshCopies.length).toBeLessThan(Math.floor(budget / payloadBytes));
    } finally {
      importer.mockRestore();
    }
    check(
      input,
      Array.from({ length: count }, () => "ffffff"),
      Array.from({ length: count }, (_, index) => index * 10),
      Array.from({ length: count }, () => 1),
    );
  });

  it("rejects multibyte mesh variants before importing beyond the UTF-8 byte budget", () => {
    const admitted = multibyteVariants("漢".repeat(32));
    expect(applyThreeMfColors(admitted)).not.toBe(admitted);
    check(
      admitted,
      ["112233", "223344", "334455", "445566", "556677", "667788"],
      [0, 10, 20, 30, 40, 50],
      Array(6).fill(1),
    );
    const input = multibyteVariants("漢".repeat(200_000));
    const importer = vi.spyOn(Document.prototype, "importNode");
    try {
      expect(applyThreeMfColors(input) === input).toBe(true);
      const copies = importer.mock.calls.filter(
        ([node]) => node instanceof Element && node.querySelector("mesh"),
      );
      expect(copies.length).toBeLessThan(5);
    } finally {
      importer.mockRestore();
    }
    check(input, Array(6).fill("ffffff"), [0, 10, 20, 30, 40, 50], Array(6).fill(1));
  });

  it("recolours multibyte variants within the byte budget and rejects their oversized neighbour", () => {
    const input = multibyteVariants("aé漢🦄".repeat(20_000));
    const normalized = applyThreeMfColors(input);
    expect(normalized === input).toBe(false);
    const selected = Object.values(unzipSync(new Uint8Array(input))).reduce(
      (total, entry) => total + entry.byteLength,
      0,
    );
    const bytes = unzipSync(new Uint8Array(normalized))["3D/3dmodel.model"];
    expect(bytes.byteLength).toBeGreaterThan(strFromU8(bytes).length);
    expect(bytes.byteLength).toBeLessThan(selected * MAX_EMITTED_RATIO + MAX_EMITTED_OVERHEAD);
    const rows = rendered(normalized);
    expect(rows.map((row) => row.hex)).toEqual([
      "112233",
      "223344",
      "334455",
      "445566",
      "556677",
      "667788",
    ]);
    expect(rows.map((row) => row.min)).toEqual([0, 10, 20, 30, 40, 50].map((x) => [x, 0, 0]));
    expect(rows.map((row) => row.max)).toEqual([1, 11, 21, 31, 41, 51].map((x) => [x, 2, 3]));
    const oversized = multibyteVariants("aé漢🦄".repeat(60_000));
    expect(applyThreeMfColors(oversized) === oversized).toBe(true);
    check(oversized, Array(6).fill("ffffff"), [0, 10, 20, 30, 40, 50], Array(6).fill(1));
  });

  it("admits large declared model sizes without multiplying recoloured mesh payloads", () => {
    const payloadBytes = 16 * 1024;
    const source = model(
      mesh(1).replace(
        "<mesh>",
        `<metadata name="payload">${"x".repeat(payloadBytes)}</metadata><mesh>`,
      ) +
        mesh(2, 2) +
        composite(100, component(1) + component(2, 10)),
      Array.from(
        { length: 3 },
        (_, index) => `<item objectid="100" transform="${transform(index * 20)}"/>`,
      ).join(""),
    );
    const input = archive({ "3D/root.model": source });
    const normalized = applyThreeMfColors(input);
    expect(normalized === input).toBe(false);
    const entries = unzipSync(new Uint8Array(normalized));
    expect(entries["3D/3dmodel.model"].byteLength).toBeGreaterThan(payloadBytes);
    expect(entries["3D/3dmodel.model"].byteLength).toBeLessThan(payloadBytes + 4096);
    const rows = rendered(normalized);
    expect(rows.map((row) => row.hex)).toEqual([
      "f53b9d",
      "4dc5a0",
      "f53b9d",
      "4dc5a0",
      "f53b9d",
      "4dc5a0",
    ]);
    expect(rows.map((row) => row.min)).toEqual([
      [0, 0, 0],
      [10, 0, 0],
      [20, 0, 0],
      [30, 0, 0],
      [40, 0, 0],
      [50, 0, 0],
    ]);
    expect(rows.map((row) => row.max)).toEqual([
      [1, 2, 3],
      [12, 2, 3],
      [21, 2, 3],
      [32, 2, 3],
      [41, 2, 3],
      [52, 2, 3],
    ]);
    // A tiny forged payload probes size admission without constructing a large DOM.
    const declaredLarge = archive({ "3D/root.model": source });
    declareOriginalSize(declaredLarge, "3D/root.model", 33 * 1024 * 1024);
    let otherCompressedBytes = 0;
    unzipSync(new Uint8Array(declaredLarge), {
      filter: (entry) => {
        if (entry.name !== "3D/root.model") otherCompressedBytes += entry.size;
        return false;
      },
    });
    const inflater = vi.spyOn(Inflate.prototype, "push");
    try {
      expect(applyThreeMfColors(declaredLarge)).toBe(declaredLarge);
      const pushedBytes = inflater.mock.calls.reduce(
        (total, [chunk]) => total + chunk.byteLength,
        0,
      );
      expect(pushedBytes).toBeGreaterThan(otherCompressedBytes);
      expect(pushedBytes).toBeLessThanOrEqual(declaredLarge.byteLength);
    } finally {
      inflater.mockRestore();
    }
  });

  it("recolours output above 32 MiB and bounds absolute shared-mesh expansion", () => {
    const payloadBytes = 33 * 1024 * 1024;
    const input = archive(
      {
        "3D/root.model": model(
          mesh(1).replace(
            "<mesh>",
            `<metadata name="payload">${"x".repeat(payloadBytes)}</metadata><mesh>`,
          ) +
            mesh(2, 2) +
            composite(100, component(1) + component(2, 10)),
        ),
      },
      false,
      0,
    );
    const normalized = applyThreeMfColors(input);
    expect(normalized === input).toBe(false);
    const entries = unzipSync(new Uint8Array(normalized));
    expect(entries["3D/3dmodel.model"].byteLength).toBeGreaterThan(32 * 1024 * 1024);
    expect(entries["3D/3dmodel.model"].byteLength).toBeLessThan(payloadBytes + 4096);
    const rows = rendered(normalized);
    expect(rows.map((row) => row.hex)).toEqual(["f53b9d", "4dc5a0"]);
    expect(rows.map((row) => row.min)).toEqual([
      [0, 0, 0],
      [10, 0, 0],
    ]);
    expect(rows.map((row) => row.max)).toEqual([
      [1, 2, 3],
      [12, 2, 3],
    ]);
    expect(MAX_EMITTED_BYTES).toBe(MAX_SELECTED_BYTES);
    const copiedPayloadBytes = Math.floor(MAX_SELECTED_BYTES / 3) + 4096;
    const oversized = archive(
      {
        "3D/root.model": model(
          mesh(1).replace(
            "<mesh>",
            `<metadata name="payload">${"x".repeat(copiedPayloadBytes)}</metadata><mesh>`,
          ) + [100, 101, 102].map((id, index) => composite(id, component(1, index * 10))).join(""),
          [100, 101, 102].map((id) => `<item objectid="${id}"/>`).join(""),
        ),
        "Metadata/model_settings.config": `<config>${[100, 101, 102].map((id, index) => config(part(1, index + 1), undefined, id)).join("")}</config>`,
      },
      false,
      0,
    );
    expect(copiedPayloadBytes * 3).toBeGreaterThan(MAX_EMITTED_BYTES);
    expect(oversized.byteLength).toBeLessThan(MAX_SELECTED_BYTES);
    const serializer = vi.spyOn(XMLSerializer.prototype, "serializeToString");
    try {
      expect(applyThreeMfColors(oversized) === oversized).toBe(true);
      expect(serializer).not.toHaveBeenCalled();
    } finally {
      serializer.mockRestore();
    }
  }, 30_000); // Real large-output and absolute-ceiling probes need headroom on slower CI runners.

  it("memoizes repeated leaf instances before deep import", () => {
    const count = 80;
    const input = archive({
      "3D/root.model": model(
        mesh(1) + mesh(2, 2),
        Array.from(
          { length: count },
          (_, index) => `<item objectid="1" transform="${transform(index * 10)}"/>`,
        ).join("") + `<item objectid="2" transform="${transform(1000)}"/>`,
      ),
      "Metadata/model_settings.config": `<config>${config(part(1, 1), undefined, 1)}${config(part(2, 2), undefined, 2)}</config>`,
    });
    const normalized = applyThreeMfColors(input);
    expect(normalized).not.toBe(input);
    const document = new DOMParser().parseFromString(
      strFromU8(unzipSync(new Uint8Array(normalized))["3D/3dmodel.model"]),
      "application/xml",
    );
    expect(document.querySelectorAll("object > mesh")).toHaveLength(2);
    const rows = check(
      input,
      [...Array.from({ length: count }, () => "f53b9d"), "4dc5a0"],
      [...Array.from({ length: count }, (_, index) => index * 10), 1000],
      [...Array.from({ length: count }, () => 1), 2],
    );
    expect(new Set(rows.slice(0, count).map((row) => row.geometry)).size).toBe(1);
  });

  it("keeps many instances of a multi-part plate coloured and shared", () => {
    const instances = 200;
    const parts = 50;
    const input = archive({
      "3D/root.model": model(
        Array.from({ length: parts }, (_, index) => mesh(index + 1, index + 1)).join("") +
          composite(
            100,
            Array.from({ length: parts }, (_, index) => component(index + 1, index * 10)).join(""),
          ),
        Array.from(
          { length: instances },
          (_, index) => `<item objectid="100" transform="${transform(index * 1000)}"/>`,
        ).join(""),
      ),
      "Metadata/model_settings.config": `<config>${config(Array.from({ length: parts }, (_, index) => part(index + 1, (index % 2) + 1)).join(""))}</config>`,
    });
    const normalized = applyThreeMfColors(input);
    expect(normalized).not.toBe(input);
    const document = new DOMParser().parseFromString(
      strFromU8(unzipSync(new Uint8Array(normalized))["3D/3dmodel.model"]),
      "application/xml",
    );
    expect(document.querySelectorAll("resources > object")).toHaveLength(parts + 1);
    check(
      input,
      Array.from({ length: instances * parts }, (_, index) => (index % 2 ? "4dc5a0" : "f53b9d")),
      Array.from(
        { length: instances * parts },
        (_, index) => Math.floor(index / parts) * 1000 + (index % parts) * 10,
      ),
      Array.from({ length: instances * parts }, (_, index) => (index % parts) + 1),
    );
  });

  it("keeps root composites separate from colliding config parts", () => {
    for (const subtype of ["normal_part", "modifier_part"]) {
      for (const inherited of [undefined, 0]) {
        const ids = subtype === "normal_part" ? [100, 2, 3, 4] : [100, 2, 3, 4, 5];
        const input = archive({
          "3D/root.model": model(
            composite(
              100,
              ids.map((id, index) => component(id, index * 10, "Objects/parts.model")).join(""),
            ),
          ),
          "3D/Objects/parts.model": model(ids.map((id, index) => mesh(id, index + 1)).join(""), ""),
          "Metadata/model_settings.config": `<config>${config(part(100, 1, subtype) + part(2, 2) + part(3, inherited) + part(5, 4), 2)}</config>`,
        });
        check(
          input,
          subtype === "normal_part"
            ? ["f53b9d", "4dc5a0", "4dc5a0", "ffffff"]
            : ["ffffff", "4dc5a0", "4dc5a0", "ffffff", "ff7a18"],
          ids.map((_, index) => index * 10),
          ids.map((_, index) => index + 1),
        );
      }
    }
  });

  it("resolves build-item paths before colliding root object ids", () => {
    for (const path of ["Objects/../Objects/plate.model", "/3D/Objects/plate.model"]) {
      const input = archive({
        "3D/root.model": model(
          mesh(1) + mesh(2, 2) + composite(100, component(1) + component(2, 10)),
          `<item objectid="100" p:path="${path}" transform="${transform(10)}"/>`,
        ),
        "3D/Objects/plate.model": model(
          mesh(1, 3) + mesh(2, 5) + composite(100, component(1) + component(2, 20)),
          "",
        ),
      });
      check(input, ["f53b9d", "4dc5a0"], [10, 30], [3, 5]);
      const document = new DOMParser().parseFromString(
        strFromU8(unzipSync(new Uint8Array(applyThreeMfColors(input)))["3D/3dmodel.model"]),
        "application/xml",
      );
      expect(document.querySelector("build > item")?.getAttributeNS(production, "path")).toBeNull();
    }
  });

  it("resolves overrides, object inheritance, zero inheritance and the default slot", () => {
    const resources =
      [1, 2, 3, 4, 5].map((id) => mesh(id, id)).join("") +
      composite(100, [1, 2, 3, 4, 5].map((id) => component(id, (id - 1) * 10)).join(""));
    check(
      archive({
        "3D/root.model": model(resources),
        "Metadata/model_settings.config": `<config>${config(part(1, 1) + part(2) + part(3, 0) + part(4, 4) + part(5, 5), 2)}</config>`,
      }),
      ["f53b9d", "4dc5a0", "4dc5a0", "ff7a18", "fefefe"],
      [0, 10, 20, 30, 40],
      [1, 2, 3, 4, 5],
    );
    for (const slot of [undefined, 0, "00"]) {
      check(
        archive({
          "Metadata/model_settings.config": `<config>${config(part(1).replace(' subtype="normal_part"', "") + part(2, 2), slot)}</config>`,
        }),
      );
    }
  });

  it("keeps invalid and unmatched parts neutral alongside valid colours", () => {
    for (const slot of ["1.5", "nope", -1, 9, 3]) {
      const resources =
        mesh(1) +
        mesh(2, 2) +
        mesh(3, 3) +
        mesh(4, 4) +
        composite(100, component(1) + component(2, 10) + component(3, 20) + component(4, 30));
      check(
        archive({
          "3D/root.model": model(resources),
          "Metadata/model_settings.config": `<config>${config(part(1, 1) + part(2, 2) + part(3, slot) + part(99, 4))}</config>`,
          "Metadata/project_settings.config": JSON.stringify({
            filament_colour: [palette[0], palette[1], "invalid"],
          }),
        }),
        ["f53b9d", "4dc5a0", "ffffff", "ffffff"],
        [0, 10, 20, 30],
        [1, 2, 3, 4],
      );
    }
    check(
      archive({
        "Metadata/model_settings.config": `<config>${config(part(1, 1) + part(2, 2), "bad")}</config>`,
      }),
    );
  });

  it("passes missing, malformed, plain and single-effective-colour files through unchanged", () => {
    check(archive());
    const cases: Record<string, string | null>[] = [
      { "Metadata/model_settings.config": null },
      { "Metadata/project_settings.config": null },
      { "Metadata/model_settings.config": "<config><broken>" },
      { "Metadata/project_settings.config": "{broken" },
      {
        "Metadata/project_settings.config": JSON.stringify({ filament_colour: ["#F53B9D", "bad"] }),
      },
      {
        "Metadata/model_settings.config": `<config>${config(part(1, 1) + part(2, 1) + part(99, 2))}</config>`,
      },
      {
        "Metadata/model_settings.config": `<config>${config(part(1, 1) + part(2, 0), 1)}</config>`,
      },
      { "Metadata/model_settings.config": `<config>${config(part(1) + part(2), "1.5")}</config>` },
    ];
    for (const overrides of cases) {
      const input = archive(overrides);
      expect(applyThreeMfColors(input)).toBe(input);
      const rows = rendered(input);
      expect(rows.map((row) => row.hex)).toEqual(["ffffff", "ffffff"]);
      expect(rows.map((row) => [row.min, row.max])).toEqual([
        [
          [0, 0, 0],
          [1, 2, 3],
        ],
        [
          [10, 0, 0],
          [12, 2, 3],
        ],
      ]);
    }
    const stl = strToU8("solid plain\nendsolid plain").buffer as ArrayBuffer;
    expect(applyThreeMfColors(stl)).toBe(stl);
  });

  it("preserves standard appearances anywhere in the archive", () => {
    check(archive());
    for (const appearance of [
      "basematerials",
      "colorgroup",
      "texture2d",
      "texture2dgroup",
      "compositematerials",
      "multiproperties",
      "metallicdisplayproperties",
      "pbmetallicdisplayproperties",
    ]) {
      const input = archive({ "3D/Objects/unused.model": model(`<${appearance} id="88"/>`, "") });
      expect(applyThreeMfColors(input)).toBe(input);
      expect(rendered(input).map((row) => [row.hex, row.min, row.max])).toEqual([
        ["ffffff", [0, 0, 0], [1, 2, 3]],
        ["ffffff", [10, 0, 0], [12, 2, 3]],
      ]);
    }
    for (const resource of [
      mesh(8, 1, 'pid="88"'),
      mesh(8).replace("<triangle v1=", '<triangle pid="88" v1='),
    ]) {
      const input = archive({ "3D/Objects/unused.model": model(resource, "") });
      expect(applyThreeMfColors(input)).toBe(input);
      check(input, ["ffffff", "ffffff"]);
    }
    const bases =
      '<basematerials id="88"><base name="red" displaycolor="#FF0000"/></basematerials>';
    const input = archive({
      "3D/root.model": model(
        bases +
          mesh(1, 1, 'pid="88" pindex="0"') +
          mesh(2, 2) +
          composite(100, component(1) + component(2, 10)),
      ),
    });
    expect(applyThreeMfColors(input)).toBe(input);
    check(input, ["ff0000", "ffffff"]);
  });

  it("normalizes nested paths and duplicate ids independently of ZIP order", () => {
    const overrides = {
      "3D/root.model": model(
        composite(
          100,
          component(10, 0, "Objects/../Objects/nested.model") +
            component(20, 10, "/3D/Objects/other.model"),
        ),
      ),
      "3D/Objects/nested.model": model(composite(10, component(1, 0, "./leaves/one.model")), ""),
      "3D/Objects/leaves/one.model": model(mesh(1), ""),
      "3D/Objects/other.model": model(composite(20, component(1, 0, "leaves/two.model")), ""),
      "3D/Objects/leaves/two.model": model(mesh(1, 5), ""),
      "Metadata/model_settings.config": `<config>${config(part(10, 1) + part(20, 2))}</config>`,
    };
    for (const reverse of [false, true])
      check(archive(overrides, reverse), ["f53b9d", "4dc5a0"], [0, 10], [1, 5]);
  });

  it("preserves repeated build and component transforms with copy-on-write colours", () => {
    const input = archive({
      "3D/root.model": model(
        mesh(1) + composite(100, component(1, 2)) + composite(200, component(1, 4)),
        `<item objectid="100" transform="${transform(10, 12, 14)}"/><item objectid="200" transform="${transform(20)}"/><item objectid="100" transform="0 2 0 -3 0 0 0 0 4 30 0 0"/>`,
      ),
      "Metadata/model_settings.config": `<config>${config(part(1), 1)}${config(part(1), 2, 200)}</config>`,
    });
    const rows = rendered(applyThreeMfColors(input));
    expect(rows.map((row) => row.hex)).toEqual(["f53b9d", "4dc5a0", "f53b9d"]);
    const bounds = [
      [
        [12, 12, 14],
        [13, 14, 17],
      ],
      [
        [24, 0, 0],
        [25, 2, 3],
      ],
      [
        [24, 4, 0],
        [30, 6, 12],
      ],
    ];
    rows.forEach((row, index) => {
      row.min.forEach((value, axis) => expect(value).toBeCloseTo(bounds[index][0][axis], 8));
      row.max.forEach((value, axis) => expect(value).toBeCloseTo(bounds[index][1][axis], 8));
    });
    expect(rows[0].geometry).toBe(rows[2].geometry);
    const xml = strFromU8(unzipSync(new Uint8Array(applyThreeMfColors(input)))["3D/3dmodel.model"]);
    const normalized = new DOMParser().parseFromString(xml, "application/xml");
    expect(normalized.querySelectorAll("object > mesh")).toHaveLength(2);
    expect(normalized.documentElement.getAttribute("unit")).toBe("millimeter");
    const ids = Array.from(normalized.querySelectorAll("resources > [id]"), (element) =>
      element.getAttribute("id"),
    );
    expect(new Set(ids).size).toBe(ids.length);
    expect(normalized.querySelectorAll("basematerials")).toHaveLength(1);
    expect(
      Array.from(normalized.querySelectorAll("component"), (element) =>
        element.getAttributeNS(production, "path"),
      ),
    ).toEqual([null, null]);
  });

  it("inherits direct-part appearance despite nested cross-file id collisions", () => {
    for (const subtype of ["normal_part", "modifier_part"]) {
      const input = archive({
        "3D/root.model": model(
          mesh(20, 2) +
            mesh(30, 3) +
            composite(
              100,
              component(10, 3, "Objects/nested.model") + component(20, 10) + component(30, 20),
            ),
        ),
        "3D/Objects/nested.model": model(composite(10, component(11, 0, "deeper.model")), ""),
        "3D/Objects/deeper.model": model(composite(11, component(20, 0, "leaf.model")), ""),
        "3D/Objects/leaf.model": model(mesh(20), ""),
        "Metadata/model_settings.config": `<config>${config(part(10, 1) + part(20, 2, subtype) + part(30, 4))}</config>`,
      });
      check(
        input,
        ["f53b9d", subtype === "normal_part" ? "4dc5a0" : "ffffff", "ff7a18"],
        [3, 10, 20],
        [1, 2, 3],
      );
    }
  });

  it("retains non-normal geometry without counting or colouring it", () => {
    const subtypes = [
      "modifier_part",
      "negative_part",
      "support_blocker",
      "support_enforcer",
      "seam_position",
      "precise_seam",
    ];
    const resources =
      Array.from({ length: 8 }, (_, index) => mesh(index + 1, index + 1)).join("") +
      composite(
        100,
        Array.from({ length: 8 }, (_, index) => component(index + 1, index * 10)).join(""),
      );
    const settings = `<config>${config(part(1, 1) + part(2, 2) + subtypes.map((subtype, index) => part(index + 3, 4, subtype)).join(""))}</config>`;
    check(
      archive({ "3D/root.model": model(resources), "Metadata/model_settings.config": settings }),
      ["f53b9d", "4dc5a0", ...subtypes.map(() => "ffffff")],
      [0, 10, 20, 30, 40, 50, 60, 70],
      [1, 2, 3, 4, 5, 6, 7, 8],
    );
    const input = archive({
      "Metadata/model_settings.config": `<config>${config(part(1, 1) + part(2, 2, "modifier_part"))}</config>`,
    });
    expect(applyThreeMfColors(input)).toBe(input);
    check(input, ["ffffff", "ffffff"]);
  });

  it("bounds selected work, rejects cycles and never inflates G-code", () => {
    const bytes = new Uint8Array(archive({ "Metadata/plate_1.gcode": "G1 X0\n".repeat(1000) }));
    const view = new DataView(bytes.buffer);
    for (let offset = 0; offset < bytes.length - 46; offset++) {
      const signature = view.getUint32(offset, true);
      if (signature !== 0x02014b50) continue;
      const name = strFromU8(
        bytes.subarray(offset + 46, offset + 46 + view.getUint16(offset + 28, true)),
      );
      if (name.endsWith(".gcode")) view.setUint16(offset + 10, 99, true);
    }
    expect(() => unzipSync(bytes)).toThrow();
    check(bytes.buffer);
    const normalized = unzipSync(new Uint8Array(applyThreeMfColors(bytes.buffer)), {
      filter: (entry) => {
        expect(entry.compression).toBe(0);
        return true;
      },
    });
    expect(Object.keys(normalized).sort()).toEqual(["3D/3dmodel.model", "_rels/.rels"]);
    const cases: Record<string, string | null>[] = [
      { "3D/root.model": model(composite(100, component(100))) },
      { "3D/root.model": model(composite(100, component(999))) },
      {
        "3D/root.model": model(
          Array.from({ length: 66 }, (_, index) =>
            composite(100 + index, component(101 + index)),
          ).join("") + mesh(166),
        ),
      },
      Object.fromEntries(
        Array.from({ length: 100 }, (_model, index) => [
          `3D/unused-${index}.model`,
          model(
            Array.from(
              { length: 100 },
              (_object, partIndex) => `<object id="${partIndex + 1}"/>`,
            ).join(""),
            "",
          ),
        ]),
      ),
      { "3D/unused.model": model(composite(8, component(1).repeat(10001)), "") },
    ];
    for (const [index, overrides] of cases.entries()) {
      const input = archive(overrides);
      expect(applyThreeMfColors(input)).toBe(input);
      if (index === 2) {
        check(input, ["ffffff"], [0], [1]);
      } else if (index < 2) {
        // The stock loader rejects cycles, missing targets/meshes and malformed model XML.
        expect(() => rendered(input)).toThrow();
      }
      // Object/component-limit inputs have no renderable meshes for the stock loader.
    }
  });
});
