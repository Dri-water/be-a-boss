// beaboss cockpit client. Speaks the tiny JSON protocol from
// transports/websocket.py. The socket/protocol layer (BeabossClient) is kept free
// of DOM concerns so any embedding UI can reuse it; the view below is this page's
// rendering over that client. All rendering is DOM-node based (never innerHTML from
// message text) so a worker's output can never inject markup.

(function () {
  "use strict";

  // ---- protocol layer (reusable) -------------------------------------------

  class BeabossClient {
    constructor(url) {
      this.url = url;
      this.threads = new Map();          // id -> {id, title, open}
      this.messages = new Map();         // id -> [{speaker, text, ts, media?}]
      this.handlers = {};                // event -> fn
      this.ws = null;
      this.dashboard = "";
    }

    on(event, fn) { this.handlers[event] = fn; return this; }
    _emit(event, arg) { if (this.handlers[event]) this.handlers[event](arg); }

    connect() {
      const ws = new WebSocket(this.url);
      this.ws = ws;
      ws.onopen = () => { this._backoff = 500; this._emit("open"); };
      ws.onclose = () => { this._emit("close"); this._reconnect(); };
      ws.onmessage = (e) => this._recv(JSON.parse(e.data));
      return this;
    }

    // The server may drop us (eviction, restart, WiFi blip). Reconnect with backoff
    // instead of dying — on reconnect the server replays recent history.
    _reconnect() {
      const delay = Math.min(this._backoff || 500, 10000);
      this._backoff = delay * 2;
      setTimeout(() => this.connect(), delay);
    }

    send(threadId, text) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: "message", thread_id: threadId, text }));
      }
    }

    // Send a raw protocol message (commands: interrupt/kill/new/approve/reject).
    sendRaw(obj) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify(obj));
      }
    }

    // Append a message to a thread locally. Used for your own outgoing message,
    // which the server does not echo back — pure state, no socket involved.
    addLocalMessage(threadId, speaker, text) {
      const list = this.messages.get(threadId) || [];
      list.push({ speaker, text, ts: Date.now() });
      this.messages.set(threadId, list);
    }

    _upsertThread(t) { this.threads.set(t.id, t); }

    _recv(msg) {
      if (msg.type === "threads") {
        this.threads.clear();
        // A snapshot arrives on every (re)connect; clear the log so the server's
        // history replay that follows rebuilds it cleanly instead of duplicating.
        this.messages.clear();
        msg.threads.forEach((t) => this._upsertThread(t));
        this._emit("threads");
      } else if (msg.type === "thread") {
        if (msg.removed) this.threads.delete(msg.id);
        else this._upsertThread({ id: msg.id, title: msg.title, open: msg.open });
        this._emit("threads");
      } else if (msg.type === "message") {
        const list = this.messages.get(msg.thread_id) || [];
        list.push({ speaker: msg.speaker, text: msg.text, ts: Date.now() });
        this.messages.set(msg.thread_id, list);
        this._emit("message", msg.thread_id);
      } else if (msg.type === "media") {
        const list = this.messages.get(msg.thread_id) || [];
        list.push({ speaker: msg.speaker, text: msg.caption || "", ts: Date.now(),
                    media: { kind: msg.kind, filename: msg.filename,
                             mime: msg.mime, data_b64: msg.data_b64 } });
        this.messages.set(msg.thread_id, list);
        this._emit("message", msg.thread_id);
      } else if (msg.type === "dashboard") {
        this.dashboard = msg.text || "";
        this._emit("dashboard");
      } else if (msg.type === "busy") {
        this._emit("busy", msg.thread_id);
      } else if (msg.type === "idle") {
        this._emit("idle", msg.thread_id);
      }
    }
  }

  // ---- markdown (safe: builds DOM nodes, never innerHTML) -------------------

  const SAFE_URL = /^(https?:|mailto:)/i;

  function inlineInto(text, parent) {
    // the link URL allows one level of balanced parens so Wikipedia/MSDN-style
    // "Foo_(bar)" links aren't truncated. The two branches are disjoint on the first
    // char (paren vs not), so the outer * stays linear — no catastrophic backtracking.
    const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\((?:[^()]|\([^()]*\))*\))/;
    let rest = String(text);
    while (rest) {
      const m = re.exec(rest);
      if (!m) { parent.appendChild(document.createTextNode(rest)); break; }
      if (m.index > 0) parent.appendChild(document.createTextNode(rest.slice(0, m.index)));
      const tok = m[0];
      if (tok[0] === "`") {
        const c = document.createElement("code");
        c.textContent = tok.slice(1, -1);
        parent.appendChild(c);
      } else if (tok.startsWith("**") || tok.startsWith("__")) {
        const b = document.createElement("strong");
        inlineInto(tok.slice(2, -2), b);
        parent.appendChild(b);
      } else if (tok[0] === "*") {
        const em = document.createElement("em");
        inlineInto(tok.slice(1, -1), em);
        parent.appendChild(em);
      } else {                                   // [label](url) — url may contain ()
        const mid = tok.indexOf("](");
        const label = tok.slice(1, mid);
        const url = tok.slice(mid + 2, tok.length - 1).trim();  // between ]( and final )
        if (SAFE_URL.test(url)) {                 // only safe schemes become links
          const a = document.createElement("a");
          a.textContent = label; a.href = url;
          a.target = "_blank"; a.rel = "noopener noreferrer";
          parent.appendChild(a);
        } else {                                  // unsafe scheme → inert text
          parent.appendChild(document.createTextNode(label));
        }
      }
      rest = rest.slice(m.index + tok.length);
    }
  }

  function renderInline(text, parent) {
    String(text).split("\n").forEach((seg, i) => {
      if (i) parent.appendChild(document.createElement("br"));
      inlineInto(seg, parent);
    });
  }

  const isListLine = (l) => /^\s*([-*+]|\d+\.)\s+/.test(l);

  function renderMarkdown(src) {
    const frag = document.createDocumentFragment();
    const lines = String(src).replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {                    // fenced code block
        const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
        if (i < lines.length) i++;                // consume closing fence
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = buf.join("\n");
        pre.appendChild(code); frag.appendChild(pre);
        continue;
      }
      const h = line.match(/^(#{1,3})\s+(.*)$/);
      if (h) {
        const el = document.createElement("h" + h[1].length);
        renderInline(h[2], el); frag.appendChild(el); i++;
        continue;
      }
      if (/^>\s?/.test(line)) {                    // blockquote
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^>\s?/, "")); i++;
        }
        const bq = document.createElement("blockquote");
        renderInline(buf.join("\n"), bq); frag.appendChild(bq);
        continue;
      }
      if (isListLine(line)) {                      // list (ul / ol)
        const ordered = /^\s*\d+\.\s+/.test(line);
        const list = document.createElement(ordered ? "ol" : "ul");
        while (i < lines.length && isListLine(lines[i])) {
          const li = document.createElement("li");
          renderInline(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ""), li);
          list.appendChild(li); i++;
        }
        frag.appendChild(list);
        continue;
      }
      if (line.trim() === "") { i++; continue; }
      const buf = [line]; i++;                     // paragraph
      while (i < lines.length && lines[i].trim() !== "" &&
             !/^```/.test(lines[i]) && !/^#{1,3}\s/.test(lines[i]) &&
             !/^>\s?/.test(lines[i]) && !isListLine(lines[i])) {
        buf.push(lines[i]); i++;
      }
      const p = document.createElement("p");
      renderInline(buf.join("\n"), p); frag.appendChild(p);
    }
    return frag;
  }

  // ---- view layer (this page) ----------------------------------------------

  const $ = (id) => document.getElementById(id);
  const fmtTime = (ts) => new Date(ts)
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  function threadIcon(id, title) {
    if (id === "general") return { emoji: "🧭", name: "Orchestrator" };
    if (id.startsWith("dm:")) return { emoji: "💬", name: title || id };
    if ((title || "").startsWith("⚙️")) {
      return { emoji: "⚙️", name: title.replace(/^⚙️\s*/, "") };
    }
    return { emoji: "▸", name: title || id };
  }

  function connect(url) {
    const client = new BeabossClient(url).connect();
    let active = null;
    const busy = new Set();               // threads whose agent is mid-turn
    const unread = new Map();             // thread id -> unseen message count

    const conn = $("conn"), status = $("status");
    const input = $("box"), submit = $("submit");

    let everOpened = false;
    client.on("open", () => {
      everOpened = true;
      conn.className = "on"; conn.title = "connected";
      status.textContent = "connected";
      input.disabled = submit.disabled = false;
    });
    client.on("close", () => {
      // Distinguish "dropped, retrying" from "never connected" (usually a wrong or
      // missing ?token=…) so a first-run mistake doesn't read as a dead bot.
      conn.className = everOpened ? "retry" : "off";
      const msg = everOpened
        ? "disconnected — reconnecting…"
        : "can't connect — is the server running, and is your ?token=… correct?";
      conn.title = status.textContent = msg;
      input.disabled = submit.disabled = true;
    });

    client.on("threads", () => {
      if (active === null && client.threads.size) {
        active = client.threads.keys().next().value;
      }
      renderThreads(); renderLog(); renderTopbar();
    });
    // The live status board — same content as Telegram's pinned message.
    client.on("dashboard", () => {
      $("dash").textContent = client.dashboard || "";
      $("dash-wrap").hidden = !client.dashboard;
    });
    // "working…" indicator: instant confirmation the turn started, before the first
    // reply. Set when the agent's turn starts (or when you hit send), cleared when
    // output arrives.
    client.on("busy", (tid) => {
      busy.add(tid);
      renderThreads();
      if (tid === active) { renderLog(); renderTopbar(); }
    });
    client.on("message", (tid) => {
      busy.delete(tid);
      if (tid === active) { renderLog(); renderTopbar(); }
      else { unread.set(tid, (unread.get(tid) || 0) + 1); renderThreads(); }
    });
    // turn-end: clear "working" even when the turn posted nothing (quiet digest),
    // so the dot / typing bubble can't stick on an idle thread forever.
    client.on("idle", (tid) => {
      if (!busy.has(tid)) return;
      busy.delete(tid);
      renderThreads();
      if (tid === active) { renderLog(); renderTopbar(); }
    });

    function switchTo(tid) {
      active = tid; unread.delete(tid);
      renderThreads(); renderLog(); renderTopbar(); input.focus();
    }

    function renderThreads() {
      const el = $("threads");
      el.textContent = "";
      for (const t of client.threads.values()) {
        const row = document.createElement("div");
        row.className = "thread" + (t.id === active ? " active" : "") +
          (t.open ? "" : " closed");
        const ic = threadIcon(t.id, t.title);
        const emoji = document.createElement("span");
        emoji.className = "emoji"; emoji.textContent = ic.emoji;
        const name = document.createElement("span");
        name.className = "name"; name.textContent = ic.name;
        row.append(emoji, name);
        if (busy.has(t.id)) {
          const d = document.createElement("span"); d.className = "dot"; row.appendChild(d);
        }
        const n = unread.get(t.id);
        if (n && t.id !== active) {
          const b = document.createElement("span"); b.className = "badge";
          b.textContent = n > 9 ? "9+" : String(n); row.appendChild(b);
        }
        row.onclick = () => switchTo(t.id);
        el.appendChild(row);
      }
    }

    function renderTopbar() {
      const t = client.threads.get(active);
      const chip = $("active-chip");
      if (!t) {
        $("active-title").textContent = "—"; $("active-sub").textContent = "";
        chip.classList.remove("show"); return;
      }
      const ic = threadIcon(t.id, t.title);
      $("active-title").textContent = ic.name;
      $("active-sub").textContent = t.id === "general" ? "orchestrator"
        : t.id.startsWith("dm:") ? "direct message"
        : (t.open ? "worker session" : "session ended");
      chip.classList.toggle("show", busy.has(active));
    }

    function renderMessage(m) {
      const role = (m.speaker && m.speaker.role) || "system";
      const wrap = document.createElement("div");
      wrap.className = "msg role-" + role;

      const head = document.createElement("div"); head.className = "head";
      const who = document.createElement("span"); who.className = "who";
      who.textContent = (m.speaker && m.speaker.emoji ? m.speaker.emoji + " " : "") +
        ((m.speaker && m.speaker.name) || "system");
      head.appendChild(who);
      if (m.ts) {
        const time = document.createElement("span");
        time.className = "time"; time.textContent = fmtTime(m.ts);
        head.appendChild(time);
      }
      wrap.appendChild(head);

      if (m.text) {
        const body = document.createElement("div"); body.className = "body";
        body.appendChild(renderMarkdown(m.text));
        wrap.appendChild(body);
      }

      if (m.media) {
        // The worker controls the MIME (filename-derived), so don't trust it: render
        // only known-safe image types inline (SVG-as-<img> can't script), and force
        // everything else to octet-stream + download so a text/html blob can't render.
        const isImg = /^image\/(png|jpe?g|gif|webp|bmp|svg\+xml)$/i.test(m.media.mime || "");
        const mime = isImg ? m.media.mime : "application/octet-stream";
        const url = "data:" + mime + ";base64," + m.media.data_b64;
        if (isImg) {
          const img = document.createElement("img");
          img.src = url; img.alt = m.media.filename; img.className = "media";
          wrap.appendChild(img);
        } else {
          const a = document.createElement("a");
          a.href = url; a.download = m.media.filename;
          a.className = "file"; a.textContent = "⬇ " + m.media.filename;
          wrap.appendChild(a);
        }
      }

      // conservative-mode 🚦 prompt → real Approve / Reject buttons (parsed from the
      // code-generated "/approve <id>" line, so no protocol change is needed).
      const appr = role === "system" && m.text && m.text.indexOf("🚦") !== -1 &&
        m.text.match(/\/approve\s+(\S+)/);
      if (appr) {
        const wid = appr[1];
        const bar = document.createElement("div"); bar.className = "actions";
        const ok = document.createElement("button");
        ok.className = "approve"; ok.textContent = "Approve " + wid;
        ok.onclick = () => client.sendRaw({ type: "approve", worker_id: wid });
        const no = document.createElement("button");
        no.className = "reject"; no.textContent = "Reject";
        no.onclick = () => client.sendRaw({ type: "reject", worker_id: wid });
        bar.append(ok, no); wrap.appendChild(bar);
      }
      return wrap;
    }

    function renderLog() {
      const log = $("log");
      log.textContent = "";
      for (const m of client.messages.get(active) || []) {
        log.appendChild(renderMessage(m));
      }
      if (busy.has(active)) {
        const t = document.createElement("div"); t.className = "typing";
        for (let k = 0; k < 3; k++) t.appendChild(document.createElement("span"));
        log.appendChild(t);
      }
      log.scrollTop = log.scrollHeight;
    }

    $("send").addEventListener("submit", (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text || active === null) return;
      if (text.startsWith("/")) {
        runCommand(text);
      } else {
        client.send(active, text);
        // The server doesn't echo your own message back — show it locally so your
        // side of the conversation is visible, not just the agents' replies.
        client.addLocalMessage(active, { role: "you", name: "You" }, text);
        busy.add(active);   // instant "working…" until the reply lands
      }
      input.value = "";
      renderLog(); renderTopbar();
    });

    // Slash-commands mirror Telegram — the web kill switch + approvals:
    // /stop /kill (this thread), /approve <id>, /reject <id>, /new <path> [name].
    function runCommand(text) {
      const parts = text.slice(1).split(/\s+/);
      const cmd = (parts[0] || "").toLowerCase();
      const rest = parts.slice(1);
      const HELP = "Just type to talk to the orchestrator. Commands: " +
        "/stop · /kill (this thread) · /approve <id> · /reject <id> · " +
        "/new <path> [name] · /reset [confirm]";
      if (cmd === "stop") client.sendRaw({ type: "interrupt", thread_id: active });
      else if (cmd === "kill") client.sendRaw({ type: "kill", thread_id: active });
      else if (cmd === "approve") client.sendRaw({ type: "approve", worker_id: rest[0] || "" });
      else if (cmd === "reject") client.sendRaw({ type: "reject", worker_id: rest[0] || "" });
      else if (cmd === "new") client.sendRaw({ type: "new", path: rest[0] || "", name: rest.slice(1).join(" ") });
      else if (cmd === "reset") client.sendRaw({ type: "reset", confirm: rest[0] === "confirm" });
      else if (cmd === "help") { client.addLocalMessage(active, { role: "system", name: "help" }, HELP); return; }
      else { client.addLocalMessage(active, { role: "system", name: "sys" }, "unknown command: /" + cmd + " — try /help"); return; }
      client.addLocalMessage(active, { role: "you", name: "You" }, text);
    }
  }

  window.beaboss = { BeabossClient, connect, renderMarkdown };
})();
