"use strict";

const fs = require("node:fs");
const http = require("node:http");
const { createRequire } = require("node:module");
const path = require("node:path");

async function buildReactFixture(entryPath, htmlPath) {
  const webRoot = path.resolve(__dirname, "../../..");
  const webRequire = createRequire(path.join(webRoot, "package.json"));
  const viteRequire = createRequire(webRequire.resolve("vite"));
  const { outputFiles } = await viteRequire("esbuild").build({
    entryPoints: [entryPath],
    bundle: true,
    write: false,
    nodePaths: [path.join(webRoot, "node_modules")],
    platform: "browser",
    format: "iife",
    jsx: "automatic",
    define: { "process.env.NODE_ENV": '"development"' },
  });
  return { html: fs.readFileSync(htmlPath, "utf8"), script: outputFiles[0].text };
}

async function startReactFixture(entryPath, htmlPath, route) {
  const { html, script } = await buildReactFixture(entryPath, htmlPath);
  const server = http.createServer((request, response) => {
    response.setHeader("Cache-Control", "no-store");
    if (request.url === "/app.js") {
      response.setHeader("Content-Type", "application/javascript; charset=utf-8");
      response.end(script);
    } else if (request.url === route) {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(html);
    } else {
      response.writeHead(404);
      response.end();
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    url: `http://127.0.0.1:${server.address().port}${route}`,
    close: () =>
      new Promise((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      }),
  };
}

module.exports = { buildReactFixture, startReactFixture };
