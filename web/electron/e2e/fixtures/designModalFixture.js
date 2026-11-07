"use strict";

const path = require("node:path");
const { buildReactFixture, startReactFixture } = require("./reactFixtureServer");

const ENTRY = path.join(__dirname, "designModalFixture.jsx");
const HTML = path.join(__dirname, "designModalFixture.html");

const buildRadixFormFixture = () => buildReactFixture(ENTRY, HTML);
const startRadixFormFixture = () => startReactFixture(ENTRY, HTML, "/modal");

module.exports = { buildRadixFormFixture, startRadixFormFixture };
