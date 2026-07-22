// Minimal beaboss WebSocket client. Speaks the tiny JSON protocol from
// transports/websocket.py. The socket/protocol layer (BeabossClient) is kept
// free of DOM concerns so a VS Code extension can reuse it with its own view;
// the DOM wiring below is this page's view over that client.

(function () {
  "use strict";

  // ---- protocol layer (reusable) -------------------------------------------

  class BeabossClient {
    constructor(url) {
      this.url = url;
      this.threads = new Map();          // id -> {id, title, open}
      this.messages = new Map();         // id -> [{speaker, text}]
      this.handlers = {};                // event -> fn
      this.ws = null;
    }

    on(event, fn) { this.handlers[event] = fn; return this; }
    _emit(event, arg) { if (this.handlers[event]) this.handlers[event](arg); }

    connect() {
      const ws = new WebSocket(this.url);
      this.ws = ws;
      ws.onopen = () => this._emit("open");
      ws.onclose = () => this._emit("close");
      ws.onmessage = (e) => this._recv(JSON.parse(e.data));
      return this;
    }

    send(threadId, text) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: "message", thread_id: threadId, text }));
      }
    }

    // Append a message to a thread locally. Used for your own outgoing message,
    // which the server does not echo back — pure state, no socket involved.
    addLocalMessage(threadId, speaker, text) {
      const list = this.messages.get(threadId) || [];
      list.push({ speaker, text });
      this.messages.set(threadId, list);
    }

    _upsertThread(t) { this.threads.set(t.id, t); }

    _recv(msg) {
      if (msg.type === "threads") {
        this.threads.clear();
        msg.threads.forEach((t) => this._upsertThread(t));
        this._emit("threads");
      } else if (msg.type === "thread") {
        this._upsertThread({ id: msg.id, title: msg.title, open: msg.open });
        this._emit("threads");
      } else if (msg.type === "message") {
        const list = this.messages.get(msg.thread_id) || [];
        list.push({ speaker: msg.speaker, text: msg.text });
        this.messages.set(msg.thread_id, list);
        this._emit("message", msg.thread_id);
      } else if (msg.type === "busy") {
        this._emit("busy", msg.thread_id);
      }
    }
  }

  // ---- view layer (this page) ----------------------------------------------

  const $ = (id) => document.getElementById(id);

  function connect(url) {
    const client = new BeabossClient(url).connect();
    let active = null;
    const busyThreads = new Set();   // threads whose agent is mid-turn

    const status = $("status");
    const box = $("box");
    const submit = $("submit");

    client.on("open", () => {
      status.textContent = "connected";
      box.disabled = submit.disabled = false;
    });
    client.on("close", () => {
      status.textContent = "disconnected";
      box.disabled = submit.disabled = true;
    });

    client.on("threads", () => {
      if (active === null && client.threads.size) {
        active = client.threads.keys().next().value;
      }
      renderThreads();
      renderLog();
    });
    // "working…" indicator: instant confirmation the message landed, even before
    // the first reply. Set when the agent's turn starts (or when you hit send),
    // cleared when output arrives.
    client.on("busy", (threadId) => {
      busyThreads.add(threadId);
      if (threadId === active) renderLog();
    });
    client.on("message", (threadId) => {
      busyThreads.delete(threadId);
      if (threadId === active) renderLog();
    });

    function renderThreads() {
      const el = $("threads");
      el.textContent = "";
      for (const t of client.threads.values()) {
        const row = document.createElement("div");
        row.className = "thread" + (t.id === active ? " active" : "") +
          (t.open ? "" : " closed");
        row.textContent = t.title;
        row.onclick = () => { active = t.id; renderThreads(); renderLog(); };
        el.appendChild(row);
      }
    }

    function renderLog() {
      const log = $("log");
      log.textContent = "";
      for (const m of client.messages.get(active) || []) {
        const wrap = document.createElement("div");
        wrap.className = "msg role-" + (m.speaker.role || "system");
        const head = document.createElement("div");
        head.className = "speaker";
        head.textContent = (m.speaker.emoji ? m.speaker.emoji + " " : "") +
          m.speaker.name;
        const body = document.createElement("div");
        body.className = "text";
        body.textContent = m.text;
        wrap.append(head, body);
        log.appendChild(wrap);
      }
      if (busyThreads.has(active)) {
        const w = document.createElement("div");
        w.className = "msg role-system busy";
        const b = document.createElement("div");
        b.className = "text";
        b.textContent = "⚙️ working…";
        w.appendChild(b);
        log.appendChild(w);
      }
      log.scrollTop = log.scrollHeight;
    }

    $("send").addEventListener("submit", (e) => {
      e.preventDefault();
      const text = box.value.trim();
      if (!text || active === null) return;
      client.send(active, text);
      // The server doesn't echo your own message back — show it locally so your
      // side of the conversation is visible, not just the agents' replies.
      client.addLocalMessage(active, { role: "you", name: "You" }, text);
      busyThreads.add(active);   // instant "working…" until the reply lands
      box.value = "";
      renderLog();
    });
  }

  window.beaboss = { BeabossClient, connect };
})();
