// Bundle the extension host entry to dist/. The webview's client.js runs in the
// browser context and is loaded directly by URI, so it is NOT bundled here —
// instead it is copied in from web/client.js at build time, so the protocol
// client has a single source of truth shared with the web app (no drift).
const esbuild = require("esbuild");
const fs = require("fs");
const path = require("path");

function syncClient() {
  const src = path.join(__dirname, "..", "web", "client.js");
  const dst = path.join(__dirname, "media", "client.js");
  fs.copyFileSync(src, dst);
  return dst;
}

esbuild
  .build({
    entryPoints: ["src/extension.js"],
    bundle: true,
    platform: "node",
    format: "cjs",
    target: "node16",
    external: ["vscode"],
    outfile: "dist/extension.js",
  })
  .then(() => {
    syncClient();
    console.log(
      "esbuild: build succeeded -> dist/extension.js (+ media/client.js copied from web/)"
    );
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
