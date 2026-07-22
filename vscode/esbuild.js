// Bundle the extension host entry to dist/. The webview scripts in media/
// (client.js and the bootstrap) run in the browser context and are loaded
// directly by URI, so they are intentionally NOT bundled here.
const esbuild = require("esbuild");

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
  .then(() => console.log("esbuild: build succeeded -> dist/extension.js"))
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
