# be-a-boss for VS Code

Puts the be-a-boss agentic chat UI inside the editor. It opens a
webview panel with the thread list, message log, and a send box — the same view
as the `web/` client, driven by the same WebSocket protocol layer.

The extension does **not** reimplement the protocol: it loads a copy of the
web client's `BeabossClient` (`media/client.js`) and wires the view over it, so
there is a single source of truth for the wire format.

## Usage

1. Start a be-a-boss server so its WebSocket endpoint is listening
   (default `ws://127.0.0.1:8765`).
2. Run the command **be-a-boss: Open** from the Command Palette.

## Setting

| Setting          | Default                  | Description                                        |
| ---------------- | ------------------------ | -------------------------------------------------- |
| `beaboss.wsUrl`  | `ws://127.0.0.1:8765`    | WebSocket URL of the be-a-boss server to connect to. |

## Build

```sh
npm install
npm run build     # esbuild bundles src/extension.js -> dist/extension.js
```

## Package a .vsix

```sh
npm run build
npx --yes @vscode/vsce package --no-dependencies
```

This produces `beaboss-<version>.vsix`. It is not published to the Marketplace;
install it locally with **Extensions: Install from VSIX…** if you want to try it.

> Before publishing for real, replace the `publisher` placeholder in
> `package.json` with your Marketplace publisher id.
