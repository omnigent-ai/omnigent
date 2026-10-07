import { describe, expect, it, vi } from "vitest";
import * as THREE from "three";
import { ThreeMFLoader } from "three/examples/jsm/loaders/3MFLoader.js";
import { strToU8, strFromU8, unzipSync, zipSync } from "three/examples/jsm/libs/fflate.module.js";
import { applyThreeMfColors } from "./threeMfColors";

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

describe("Bambu filament colours through the stock 3MF loader", () => {
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
    try {
      for (const name of ["Metadata/model_settings.config", "3D/root.model"]) {
        const input = archive(
          name.endsWith(".config")
            ? { [name]: `<config>${" ".repeat(33 * 1024 * 1024)}</config>` }
            : {},
          false,
          0,
        );
        forgeSize(input, name, 1);
        parser.mockClear();
        expect(applyThreeMfColors(input)).toBe(input);
        expect(parser.mock.calls.every(([text]) => String(text).length <= 32 * 1024 * 1024)).toBe(
          true,
        );
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
        expect(parser.mock.calls).toHaveLength(size === 1 ? 1 : 0);
        check(input, ["ffffff", "ffffff"]);
      }
    } finally {
      parser.mockRestore();
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

  it("bounds cumulative colour-variant expansion before cloning meshes", () => {
    const count = 35;
    const input = archive({
      "3D/root.model": model(
        mesh(1).replace(
          "<mesh>",
          `<metadata name="large">${"x".repeat(1024 * 1024)}</metadata><mesh>`,
        ) +
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
    const importer = vi.spyOn(Document.prototype, "importNode");
    try {
      expect(applyThreeMfColors(input) === input).toBe(true);
      const meshCopies = importer.mock.calls.filter(
        ([node]) => node instanceof Element && node.getAttribute("id") === "1",
      );
      expect(meshCopies.length).toBeGreaterThan(0);
      expect(meshCopies.length).toBeLessThan(32);
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

  it("recolours large selected models without multiplying their mesh payload", () => {
    const input = archive({
      "3D/root.model": model(
        mesh(1).replace(
          "<mesh>",
          `<metadata name="large">${"x".repeat(33 * 1024 * 1024)}</metadata><mesh>`,
        ) +
          mesh(2, 2) +
          composite(100, component(1) + component(2, 10)),
      ),
    });
    const normalized = applyThreeMfColors(input);
    expect(normalized === input).toBe(false);
    const entries = unzipSync(new Uint8Array(normalized));
    expect(entries["3D/3dmodel.model"].byteLength).toBeGreaterThan(32 * 1024 * 1024);
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
  });

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
    const importer = vi.spyOn(Document.prototype, "importNode");
    let normalized: ArrayBuffer;
    try {
      normalized = applyThreeMfColors(input);
      expect(normalized).not.toBe(input);
      expect(
        importer.mock.calls.filter(
          ([node]) => node instanceof Element && node.getAttribute("id") === "1",
        ),
      ).toHaveLength(1);
    } finally {
      importer.mockRestore();
    }
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
      {
        "3D/unused.model": model(
          Array.from({ length: 10001 }, (_, index) => `<object id="${index + 1}"/>`).join(""),
          "",
        ),
      },
      { "3D/unused.model": model(composite(8, component(1).repeat(10001)), "") },
      // Whitespace has no model root, so the stock loader cannot render this byte-limit input.
      { "3D/oversized.model": " ".repeat(32 * 1024 * 1024 + 1) },
    ];
    for (const [index, overrides] of cases.entries()) {
      const input = archive(overrides);
      expect(applyThreeMfColors(input)).toBe(input);
      if (index === 2) {
        check(input, ["ffffff"], [0], [1]);
      } else if (index !== 5) {
        // The stock loader rejects cycles, missing targets/meshes and malformed model XML.
        expect(() => rendered(input)).toThrow();
      }
    }
  });
});
