// be-a-boss VS Code extension host.
//
// Opens a webview panel that hosts the same chat UI as web/. The panel loads
// the copied media/client.js (the shared BeabossClient protocol layer) and
// calls beaboss.connect(wsUrl) — so the WebSocket protocol is reused verbatim,
// never reimplemented here. The only host-side responsibilities are: register
// the command, build the webview HTML, and pass in the resolved ws URL.

const vscode = require("vscode");

/** @param {vscode.ExtensionContext} context */
function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("beaboss.open", () => openPanel(context))
  );
}

/** @type {vscode.WebviewPanel | undefined} */
let panel;

/** @param {vscode.ExtensionContext} context */
function openPanel(context) {
  if (panel) {
    panel.reveal();
    return;
  }

  const mediaUri = vscode.Uri.joinPath(context.extensionUri, "media");
  panel = vscode.window.createWebviewPanel(
    "beaboss",
    "be-a-boss",
    vscode.ViewColumn.One,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [mediaUri],
    }
  );
  panel.onDidDispose(() => {
    panel = undefined;
  });

  const wsUrl = vscode.workspace
    .getConfiguration("beaboss")
    .get("wsUrl", "ws://127.0.0.1:8765");

  panel.webview.html = renderHtml(panel.webview, mediaUri, wsUrl);
}

/**
 * @param {vscode.Webview} webview
 * @param {vscode.Uri} mediaUri
 * @param {string} wsUrl
 */
function renderHtml(webview, mediaUri, wsUrl) {
  const clientUri = webview.asWebviewUri(
    vscode.Uri.joinPath(mediaUri, "client.js")
  );
  const cssUri = webview.asWebviewUri(
    vscode.Uri.joinPath(mediaUri, "main.css")
  );
  const nonce = makeNonce();

  // connect-src permits ws:/wss: so the configurable server URL can be reached.
  const csp = [
    "default-src 'none'",
    `style-src ${webview.cspSource}`,
    `script-src 'nonce-${nonce}' ${webview.cspSource}`,
    "connect-src ws: wss:",
  ].join("; ");

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="${cssUri}">
  <title>be-a-boss</title>
</head>
<body>
  <aside>
    <h1>THREADS</h1>
    <div id="threads"></div>
    <div id="status">connecting…</div>
  </aside>
  <main>
    <div id="log"></div>
    <form id="send">
      <input id="box" placeholder="Message…" autocomplete="off" disabled>
      <button id="submit" disabled>Send</button>
    </form>
  </main>
  <script nonce="${nonce}" src="${clientUri}"></script>
  <script nonce="${nonce}">
    // Reuse the shared client's view wiring over BeabossClient, pointed at the
    // URL resolved from the beaboss.wsUrl setting by the extension host.
    beaboss.connect(${JSON.stringify(wsUrl)});
  </script>
</body>
</html>`;
}

function makeNonce() {
  const chars =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let out = "";
  for (let i = 0; i < 32; i++) {
    out += chars[Math.floor(Math.random() * chars.length)];
  }
  return out;
}

module.exports = { activate, deactivate: () => {} };
