"""The engine: owns all sessions, routes messages, exposes the orchestrator.

Threads and roles:
- orchestrator thread ("the office", transport's main thread): human <-> orchestrator
- worker threads: a visible pair — the orchestrator drives a worker session, and
  everything both say is posted to the thread. The human may interject; the
  message reaches the worker as input and the orchestrator via its inbox.
- direct threads: the original beaboss model (human <-> session), unchanged.

Supervision is checkpoint-based: worker turn-ends and human interjections land in
an inbox; each worker turn-end wakes the orchestrator with the accumulated digest.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, create_sdk_mcp_server, tool

from .agent_backend import CodexBackend
from .names import pick_name
from .ports import InboundMessage, MediaIn, Outbound, Speaker, SYSTEM, Transport
from .session import CoreSession, DEFAULT_SESSION_APPEND
from .store import CoreStore, ThreadRecord
from . import worktrees

log = logging.getLogger("beaboss.core.engine")

ORCHESTRATOR_EMOJI = "🧭"
WORKER_EMOJI = "⚙️"
MAX_INBOX = 200  # bound the supervision backlog if the orchestrator can't drain it

CODE_PHILOSOPHY = (
    "Code-quality bar (non-negotiable): robustness through SIMPLICITY. Prefer the "
    "simplest solution that works — simple code is more observable, robust, and "
    "maintainable. Build for extension and modularity (small, composable pieces "
    "with clear seams), but do NOT over-engineer: no speculative abstraction, no "
    "indirection you don't need yet. When in doubt, pick the boring, obvious "
    "solution, and let the code be self-documenting (comment intent, not mechanics)."
)

ORCHESTRATOR_APPEND = (
    "You are the ORCHESTRATOR of a small software organisation — the boss's right "
    "hand. Be the colleague they can count on: genuinely on top of everything, "
    "proactive and precise, honest to a fault. You know the code and the state of "
    "every task without being asked. You do not write project code yourself — you run "
    "a team of worker agents and use your fleet tools to get work done:\n"
    "- mcp__fleet__list_repos() — see the available repositories (path + one-liner)\n"
    "- mcp__fleet__inspect_repo(repo) — look inside a repo before you brief or "
    "review: its guide docs (AGENTS.md/CLAUDE.md/README), layout, likely test "
    "command. You can also Read/Grep any repo directly at its path\n"
    "- mcp__fleet__spawn_worker(repo, task) — hire a worker: creates a visible "
    "thread and an isolated git worktree, briefs them, and they start working\n"
    "- mcp__fleet__message_worker(worker_id, text) — speak to a worker (your message "
    "is visible in their thread)\n"
    "- mcp__fleet__worker_status(worker_id?) — current fleet state\n"
    "- mcp__fleet__review_worker(worker_id) — inspect a worker's committed diff and "
    "which delivery routes are available; use it to show the boss the change\n"
    "- mcp__fleet__run_checks(worker_id, command) — actually RUN the repo's tests/"
    "build/lint in the worker's copy and get the real exit code + output. This is "
    "how you VERIFY work passes — never take a worker's 'tests pass' on faith\n"
    "- mcp__fleet__deliver_worker(worker_id, method) — land the work: 'merge' (local "
    "merge into the checkout) or 'pr' (open a GitHub PR). Only after the boss approves\n"
    "- mcp__fleet__dismiss_worker(worker_id) — end a worker: clean worktrees are "
    "removed, dirty ones are preserved and reported\n\n"
    "Prime directives:\n"
    "1. Never modify a project yourself — workers change projects, you read and "
    "direct.\n"
    "2. Never merge or discard work without the boss's explicit word.\n"
    "3. Never dismiss a worker whose work isn't committed/landed — a refused "
    "teardown is a stop-and-investigate, not an obstacle.\n"
    "4. Report outcomes faithfully. If work failed, say so plainly with the "
    "evidence.\n"
    "5. NEVER claim an action you have not taken. 'Nova is on it' is only true if "
    "spawn_worker/message_worker actually returned success — in this turn, or "
    "visible in the [fleet right now: …] line that accompanies each message from "
    "the boss (that line is code-generated ground truth; trust it over your "
    "memory). When the boss asks for something, CALL THE TOOL in the same turn, "
    "then report what actually happened — or say plainly that you haven't started. "
    "A claimed action that didn't happen is the worst possible failure.\n\n"
    "Where you work:\n"
    "- The boss talks to you in the group's #general or by DM — same you either way; "
    "just reply wherever they spoke (a DM keeps small talk out of #general).\n"
    "- #general also carries a LIVE STATUS BOARD, kept current for you automatically "
    "(what's running, blocked, awaiting approval). Don't post status chatter there — "
    "the board already shows it; speak up only when something needs a human's eye.\n"
    "- Each worker gets its own topic in the group; the boss watches and can "
    "interject there — treat that as authoritative.\n\n"
    "Be a technical lead, not a message router:\n"
    "- Before you brief a worker or judge their work, UNDERSTAND the codebase. "
    "inspect_repo for its guide docs, layout, and test command, and Read/Grep the "
    "repo directly at its path when you need to (you never edit it yourself — that's "
    "the workers' job — but you know it cold). Manage from real knowledge of the code.\n"
    "- A brief must show you looked: name the real files, the real stack, the "
    "existing conventions and the actual test command — not vague goals. Review a "
    "worker's diff on its merits, not on their say-so. A brief or a review you could "
    "have written WITHOUT opening the repo isn't good enough.\n\n"
    "How to brief a worker:\n"
    "- A brief must be SELF-CONTAINED: the worker knows nothing about this chat. "
    "State the repo context, the concrete goal, constraints, acceptance criteria, and "
    "a definition of done that demands PROOF — the passing test output shown, the "
    "build log, a screenshot of the working result where there's something to see, "
    "committed. 'It works' is not done; 'here's the green test run and a screenshot' is.\n"
    "- One worker = one task. Split independent work across workers; they run in "
    "parallel in isolated worktrees, so same-repo parallelism is safe.\n"
    "- Steer with short messages; put long instructions in the brief, not drip-fed.\n\n"
    "Supervision:\n"
    "- You are woken with [fleet inbox] digests when a worker finishes a turn, "
    "gets blocked, needs a decision, or the boss interjects in a worker thread. "
    "React to the digest: answer the worker, re-brief, dismiss, or report to the "
    "boss. Do not poll; do not micro-manage a working worker.\n"
    "- A worker's 'STATUS: blocked' means they need YOUR help now. A "
    "'needs-decision' belongs to the boss — relay it with options and your "
    "recommendation.\n"
    "- Escalate to the boss: decisions that are theirs (product choices, merges, "
    "unclear requirements), anything destructive/irreversible/security-sensitive, "
    "and finished work ready for review. Do NOT surface routine progress, "
    "retries, or internal mechanics.\n\n"
    "Delivery (the last mile — never skip it):\n"
    "- A finished task must not dead-end on a branch. review_worker to see the diff, "
    "then show the boss the actual change (a short summary plus the key parts of the "
    "diff) with your recommendation.\n"
    "- VERIFY before you deliver: run_checks with the repo's real test/build command "
    "(e.g. 'uv run pytest', 'npm test') and read the actual result — don't trust a "
    "worker's word that it passes. If checks fail, send the worker back to fix them; "
    "you can't deliver failing work. Show the boss the real check result alongside "
    "the diff.\n"
    "- Call deliver_worker to REQUEST landing it (method 'pr' when a remote + gh are "
    "available so it stays reviewable, else 'merge'). You do NOT land work yourself — "
    "deliver_worker asks the boss, and only their /approve command actually merges or "
    "opens the PR. Tell them they need to /approve <worker>; never claim it's landed "
    "until you see the delivery confirmation.\n"
    "- Self-development: if the repo a worker is changing IS this very system, prefer "
    "the 'pr' route so the change is reviewed before it can affect the running "
    "instance, and never deliver a change to it without green run_checks.\n"
    "- After a confirmed delivery you may dismiss the worker.\n\n"
    "Talk in outcomes, not mechanics — and SHOW, don't tell. The boss cares about the "
    "project and the RESULT, not your internals: say 'isolated copy' not 'worktree', "
    "'instructions' not 'brief', 'cleanup' not 'teardown', name workers only when it "
    "matters. They want to SEE real results, not read about implementation. Note how "
    "you see things: you supervise in TEXT — you get each worker's turn summary and can "
    "pull their diff with review_worker — while the VISUAL proof (screenshots a worker "
    "posts) lands in that worker's own thread, which the boss can open. So lead with the "
    "concrete proof you actually hold (the passing test output, the diff), and point the "
    "boss to the worker's thread for anything visual — don't claim to show a screenshot "
    "you can't see. Never relay a worker's 'done'/'LGTM' at face value: require the "
    "evidence (make them post the test output and a screenshot in their thread), then "
    "surface it or point to it. If a worker claims done without proof, send them back "
    "for it before you report up.\n\n"
    "The boss can see every worker thread and may talk in them directly; treat "
    "their word there as authoritative context for you and the worker both.\n"
    "Keep replies to the boss short and information-dense. No flattery. An empty "
    "queue is a healthy resting state — never invent work.\n\n"
    "Every brief you write must carry the code-quality bar below — workers build "
    "to it, and you hold them to it on review:\n"
) + CODE_PHILOSOPHY

WORKER_APPEND_EXTRA = (
    "\n\nYou are a WORKER on a small team. Your manager (the orchestrator) briefs "
    "you and supervises via this thread; the boss (a human) can read everything "
    "and may interject directly — treat boss messages as authoritative.\n"
    "- First, verify isolation: run `git rev-parse --show-toplevel` — you should "
    "be in your own worktree (branch worker/<your-id>), not the primary checkout. "
    "If you find yourself in the primary checkout, STOP and report it.\n"
    "- Work autonomously toward the brief's definition of done. Commit on your "
    "branch with clear messages as you go; your branch outlives you.\n"
    "- Act only on your brief and follow-ups. Never start surveys or extra "
    "'improvements' on your own initiative.\n"
    "- If you hit the same obstacle twice, stop and report blocked — don't grind.\n"
    "- If a decision belongs to a human (product choice, destructive action, "
    "ambiguous requirement), stop and ask — do not guess.\n"
    "- Prove your work with REAL evidence, never a bare claim. Everything you post "
    "lands in your own thread, which the boss is watching — so put the proof there: "
    "paste the actual command output (the passing test run, the build log), and when "
    "there is something to SEE, screenshot it and send it with mcp__chat__send_photo "
    "(the running app, the rendered page, a chart, the green test summary). It shows "
    "up for the boss in your thread. A result the boss can see beats any description — "
    "'tests pass' without the output shown does not count as done.\n"
    "- End every reply with one line, chosen honestly (a wrong 'done' is worse "
    "than 'blocked'):\n"
    "  STATUS: done | working | blocked: <what you need> | "
    "needs-decision: <the options>\n\n"
) + CODE_PHILOSOPHY


# --- repo grounding (so the orchestrator manages from knowledge, not vibes) ----


def _read_doc(path: Path, limit: int) -> str:
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return (text[:limit] + "\n…(truncated — read the file directly for more)…"
            if len(text) > limit else text)


def _repo_hint(repo: Path) -> str:
    """One line describing a repo, for the repo list — first real line of its guide."""
    for doc in ("AGENTS.md", "README.md"):
        text = _read_doc(repo / doc, limit=600)
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line and not line.startswith("![") and not line.startswith("<"):
                return line[:140]
    return ""


def _top_level(repo: Path) -> str:
    try:
        entries = sorted(repo.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as e:
        return f"(couldn't list: {e})"
    rows = []
    for p in entries:
        if p.name.startswith(".") and p.name != ".github":
            continue
        rows.append(f"- {p.name}/" if p.is_dir() else f"- {p.name}")
    return "\n".join(rows[:40]) or "(empty)"


def _test_hint(repo: Path) -> str:
    """A best-guess check command from the repo's shape. A HINT — the orchestrator
    still verifies by actually running it via run_checks on a worker."""
    if (repo / "pyproject.toml").is_file():
        return "uv run pytest"
    if (repo / "package.json").is_file():
        return "npm test"
    if (repo / "Cargo.toml").is_file():
        return "cargo test"
    if (repo / "go.mod").is_file():
        return "go test ./..."
    if (repo / "Makefile").is_file():
        return "make test"
    return ""


class Engine:
    def __init__(self, settings, store: CoreStore):
        self.settings = settings
        self.store = store
        self.transport: Transport | None = None
        self.sessions: dict[str, CoreSession] = {}
        self._inbox: list[str] = []          # pending supervision notes for the orchestrator
        self._waking = False                 # digest wake in flight
        self._session_locks: dict[str, asyncio.Lock] = {}  # per-thread start lock
        self._pending_delivery: dict[str, str] = {}        # worker_id -> method, awaiting /approve
        self._last_dashboard = ""                          # last rendered board (skip no-op edits)
        # Where the boss last spoke to the orchestrator (#general or a DM). Digest
        # replies and approval prompts follow the boss there instead of stranding
        # the conversation in #general while their DM goes silent.
        self._last_boss_thread = "general"
        # The thread that is the orchestrator's "office". Transports may override;
        # the Telegram adapter's General topic maps to "general".
        self.main_thread = "general"

    # ---- wiring ----------------------------------------------------------

    def attach_transport(self, transport: Transport) -> None:
        self.transport = transport

    async def _post(self, out: Outbound) -> None:
        assert self.transport is not None
        await self.transport.post(out)

    async def _busy(self, thread_id: str) -> None:
        if self.transport is not None:
            await self.transport.indicate_busy(thread_id)

    # ---- speakers --------------------------------------------------------

    def orchestrator_speaker(self) -> Speaker:
        return Speaker(role="orchestrator", name=self.settings.bot_name,
                       emoji=ORCHESTRATOR_EMOJI)

    @staticmethod
    def worker_speaker(name: str) -> Speaker:
        return Speaker(role="worker", name=name, emoji=WORKER_EMOJI)

    def _is_orchestrator_thread(self, thread_id: str) -> bool:
        """Where you talk to the (single) orchestrator: the shared #general, or any
        DM. Both drive the same one orchestrator; its reply goes back to whichever
        you used, so a DM keeps chatter out of #general — no separate 'office'."""
        return thread_id == self.main_thread or thread_id.startswith("dm:")

    def _fleet_snapshot(self) -> str:
        """One compact line of ground truth, injected into every boss turn."""
        rows = []
        for tid, rec in self.store.workers().items():
            if rec.worker_status == "dismissed":
                continue
            live = self.sessions.get(tid)
            run = live.status if live else "dormant"
            state = rec.worker_status or "working"
            rows.append(f"{rec.worker_id}={state}/{run} on {Path(rec.repo).name}")
        return "; ".join(rows) if rows else "no workers exist"

    # ---- inbound routing -------------------------------------------------

    async def on_inbound(self, msg: InboundMessage) -> None:
        # Talking to the orchestrator (#general or any DM) → the one orchestrator
        # session, replying back to wherever you spoke.
        if self._is_orchestrator_thread(msg.thread_id):
            await self._ensure_orchestrator(self.main_thread)
            session = await self._ensure_session(
                self.main_thread, self.store.get(self.main_thread))
            if session is None:
                return
            self._last_boss_thread = msg.thread_id
            # Ground every boss turn in reality: a code-generated snapshot of the
            # actual fleet rides along with the message, so the orchestrator can't
            # honestly claim "Nova is on it" when no one is.
            text = f"[fleet right now: {self._fleet_snapshot()}]\n{msg.text}"
            if msg.media:
                await session.submit_media(text, msg.media, reply_to=msg.thread_id)
            else:
                await session.submit(text, reply_to=msg.thread_id)
            return

        rec = self.store.get(msg.thread_id)
        if rec is None:
            await self._post(Outbound(
                thread_id=msg.thread_id, speaker=SYSTEM,
                text="This thread isn't active. Talk to the orchestrator in #general "
                     "or a DM, or use /new for a direct session.",
            ))
            return

        if rec.role == "worker" and rec.worker_status == "dismissed":
            # Its worktree was torn down at dismiss; don't resurrect it into a gone
            # workspace (or bring a dismissed worker back to life).
            await self._post(Outbound(
                thread_id=msg.thread_id, speaker=SYSTEM,
                text=(f"{rec.name} was dismissed — its workspace is gone (any work is "
                      f"on branch worker/{rec.worker_id}). Ask the orchestrator to "
                      f"hire a fresh worker."),
            ))
            return

        session = await self._ensure_session(msg.thread_id, rec)
        if session is None:
            return

        if rec.role == "worker":
            await self._interject(msg, rec, session)
            return

        # direct session: plain turn
        if msg.media:
            await session.submit_media(msg.text, msg.media)
        else:
            await session.submit(msg.text)

    async def _interject(self, msg: InboundMessage, rec: ThreadRecord,
                         session: CoreSession) -> None:
        """Boss speaks inside a worker thread: worker hears it now, orchestrator
        sees it in the next digest."""
        who = msg.sender_name or "the boss"
        text = (f"[Interjection from {who} — visible to you and the orchestrator]: "
                f"{msg.text}")
        if msg.media:
            await session.submit_media(text, msg.media)
        else:
            await session.submit(text)
        self._note(f"{who} said in {rec.worker_id}'s thread: {msg.text}")

    # ---- session management ---------------------------------------------

    async def _ensure_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession | None:
        session = self.sessions.get(thread_id)
        if session is not None and session.alive:
            return session
        # Serialize per thread: two coroutines (a transport handler + a worker's wake,
        # or two fleet calls) must not both build a session for one thread — the
        # loser's live subprocess would leak untracked. Also the point where we
        # replace a zombie (dead run task / failed reconnect) instead of reusing it.
        lock = self._session_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            session = self.sessions.get(thread_id)
            if session is not None and session.alive:
                return session
            if session is not None:                    # a zombie/errored session
                self.sessions.pop(thread_id, None)
                try:
                    await session.stop()
                except Exception:  # noqa: BLE001
                    pass
            try:
                if rec.role == "orchestrator":
                    session = self._make_orchestrator_session(thread_id, rec)
                elif rec.role == "worker":
                    session = self._make_worker_session(thread_id, rec)
                else:
                    session = self._make_direct_session(thread_id, rec)
                await session.start()
            except Exception as e:  # noqa: BLE001
                log.exception("failed to start session thread=%s", thread_id)
                await self._post(Outbound(
                    thread_id=thread_id, speaker=SYSTEM,
                    text=f"⚠️ couldn't start this session: {e}",
                ))
                return None
            self.sessions[thread_id] = session
            return session

    def _sid_saver(self, thread_id: str):
        def save(sid: str) -> None:
            self.store.update(thread_id, session_id=sid)
        return save

    def _make_direct_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        return CoreSession(
            thread_id=thread_id, cwd=Path(rec.cwd),
            speaker=Speaker(role="direct", name=self.settings.bot_name),
            settings=self.settings, post=self._post, busy=self._busy,
            on_session_id=self._sid_saver(thread_id), session_id=rec.session_id,
        )

    def _make_orchestrator_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        home = self.settings.state_dir / "orchestrator-home"
        home.mkdir(parents=True, exist_ok=True)
        base = (self.settings.session_system_append
                if self.settings.session_system_append is not None else "")
        append = (base + "\n\n" if base else "") + ORCHESTRATOR_APPEND
        return CoreSession(
            thread_id=thread_id, cwd=home,
            speaker=self.orchestrator_speaker(),
            settings=self.settings, post=self._post, busy=self._busy,
            on_session_id=self._sid_saver(thread_id), session_id=rec.session_id,
            system_append=append,
            extra_mcp_servers={"fleet": self._build_fleet_server()},
            final_only=True,  # text the boss one clean reply, don't narrate
        )

    def _make_worker_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        cwd = Path(rec.cwd)
        # The worker's full system prompt = the base env note + the worker role note.
        base = (self.settings.session_system_append
                if self.settings.session_system_append is not None
                else DEFAULT_SESSION_APPEND)
        worker_append = base + WORKER_APPEND_EXTRA
        # A worker may run on Codex instead of Claude; the choice lives here (the
        # single worker-construction point). Codex needs the prompt handed in (it has
        # no system-prompt channel) and the persisted id to resume across restarts.
        backend = (
            CodexBackend(cwd, system_prompt=worker_append, resume_id=rec.session_id)
            if self.settings.agent_backend == "codex"
            else None  # None => CoreSession builds the default ClaudeAgentBackend
        )
        session = CoreSession(
            thread_id=thread_id, cwd=cwd,
            speaker=self.worker_speaker(rec.name),
            settings=self.settings, post=self._post, busy=self._busy,
            on_session_id=self._sid_saver(thread_id), session_id=rec.session_id,
            system_append=worker_append,
            backend=backend,
        )
        session.on_turn_done = self._on_worker_turn_done
        return session

    async def _ensure_orchestrator(self, thread_id: str) -> None:
        if self.store.get(thread_id) is None:
            self.store.put(thread_id, ThreadRecord(
                role="orchestrator", name="orchestrator"))
        self.store.set_orchestrator_thread(thread_id)

    # ---- supervision inbox ----------------------------------------------

    def _note(self, text: str) -> None:
        self._inbox.append(text)
        if len(self._inbox) > MAX_INBOX:
            drop = len(self._inbox) - MAX_INBOX
            self._inbox = self._inbox[-MAX_INBOX:]
            log.warning("inbox exceeded %d notes; dropped %d oldest", MAX_INBOX, drop)
        log.info("inbox note: %s", text[:160])

    # ---- dashboard (the shared #general status board) -------------------

    def _render_dashboard(self) -> str:
        """A deterministic snapshot of the fleet, rendered from the store in code
        (never authored by the LLM), so the board is always exactly true."""
        workers = [r for r in self.store.workers().values()
                   if r.worker_status != "dismissed"]

        def bucket(r: ThreadRecord) -> str:
            if r.worker_id in self._pending_delivery:
                return "approve"
            if r.worker_status == "blocked":
                return "blocked"
            if r.worker_status == "delivered":
                return "delivered"
            if r.worker_status == "done":
                return "review"
            return "running"

        cats: dict[str, list[ThreadRecord]] = {
            "running": [], "review": [], "approve": [], "blocked": [], "delivered": []}
        for r in workers:
            cats[bucket(r)].append(r)

        def line(r: ThreadRecord) -> str:
            return f"  • {r.name} · {Path(r.repo).name} — {r.task[:56]}"

        out = [f"📋 {self.settings.bot_name} — live status",
               f"🟢 {len(cats['running'])} running   🔎 {len(cats['review'])} in review"
               f"   🚦 {len(cats['approve'])} to approve   ⛔ {len(cats['blocked'])} blocked"]
        if cats["approve"]:
            out += ["", "🚦 Awaiting your approval:"] + [
                f"  • {r.name} · {Path(r.repo).name} — /approve {r.worker_id}"
                for r in cats["approve"]]
        if cats["blocked"]:
            out += ["", "⛔ Blocked (need you):"] + [line(r) for r in cats["blocked"]]
        if cats["running"]:
            out += ["", "🟢 Running:"] + [line(r) for r in cats["running"]]
        if cats["review"]:
            out += ["", "🔎 Done, awaiting review:"] + [line(r) for r in cats["review"]]
        if cats["delivered"]:
            out += ["", "✅ Recently delivered:"] + [
                f"  • {r.name} · {Path(r.repo).name}" for r in cats["delivered"][-5:]]
        if not workers:
            out += ["", "idle — nothing running. Post here or DM me to start something."]
        return "\n".join(out)

    async def _refresh_dashboard(self) -> None:
        """Re-render the board and push it if it changed. A no-op on transports that
        don't support a dashboard (web), and never allowed to break the flow."""
        fn = getattr(self.transport, "update_dashboard", None)
        if fn is None:
            return
        text = self._render_dashboard()
        if text == self._last_dashboard:
            return
        self._last_dashboard = text
        try:
            await fn(text)
        except Exception:  # noqa: BLE001
            log.exception("dashboard refresh failed")

    async def _on_worker_turn_done(self, session: CoreSession, result: ResultMessage) -> None:
        rec = self.store.get(session.thread_id)
        if rec is None:
            return
        full = (result.result or "").strip()
        # Parse the STATUS from the FULL reply — the protocol puts it on the last
        # line, which a digest-sized truncation would cut off (that bug silently
        # froze workers at "working" and poisoned the fleet snapshot). Prefer a
        # properly-anchored last line so a worker QUOTING the protocol mid-text
        # can't be misread; fall back to a full-text scan.
        last_line = next(
            (ln.strip().lower() for ln in reversed(full.splitlines()) if ln.strip()), "")
        status_src = last_line if last_line.startswith("status:") else full.lower()
        if "status: done" in status_src or status_src.startswith("status:done"):
            self.store.update(session.thread_id, worker_status="done")
        elif ("status: blocked" in status_src or "status: needs-decision" in status_src
              or status_src.startswith("status:blocked")):
            self.store.update(session.thread_id, worker_status="blocked")
        tail = full if len(full) <= 600 else full[:600] + "…"
        status = "errored" if result.is_error else "finished a turn"
        self._note(f"worker {rec.worker_id} ({rec.name}, task: {rec.task[:80]}) "
                   f"{status}: {tail or '(no text)'}")
        await self._refresh_dashboard()
        await self._wake_orchestrator()

    # Coalescing window: near-simultaneous worker events (e.g. two workers finish
    # together, or a boss interjection followed by the worker's reply) become one
    # orchestrator turn instead of several.
    WAKE_COALESCE_SECS = 2.0

    async def _wake_orchestrator(self) -> None:
        """Deliver the accumulated inbox to the orchestrator as one digest turn."""
        if self._waking or not self._inbox:
            return
        othread = self.store.orchestrator_thread
        if othread is None:
            return
        rec = self.store.get(othread)
        if rec is None:
            return
        # Claim the wake BEFORE any await, or a second worker finishing during the
        # (suspending) session start passes the guard and double-drains the inbox.
        self._waking = True
        try:
            session = await self._ensure_session(othread, rec)
            if session is None:
                return
            # Loop-drain: a worker finishing while we're mid-digest appends to
            # _inbox; the while-check re-runs with no await between it and the
            # `finally` below, so no completion note can be stranded.
            while self._inbox:
                await asyncio.sleep(self.WAKE_COALESCE_SECS)
                notes, self._inbox = self._inbox, []
                if not notes:
                    break
                digest = "[fleet inbox]\n" + "\n".join(f"- {n}" for n in notes)
                # reply where the boss actually is, not into a silent #general
                await session.submit(digest, reply_to=self._last_boss_thread)
        finally:
            self._waking = False

    # ---- fleet tools (the orchestrator's powers) -------------------------

    def _build_fleet_server(self):
        engine = self

        def ok(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}]}

        def err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        @tool(
            "list_repos",
            "List the repositories available under the projects root, with their "
            "path and a one-line description. inspect_repo one before you brief.",
            {"type": "object", "properties": {}},
        )
        async def list_repos(args: dict[str, Any]) -> dict[str, Any]:
            root = engine.settings.projects_root
            try:
                rows = []
                for p in sorted(root.iterdir()):
                    if not p.is_dir() or p.name.startswith("."):
                        continue
                    if (p / ".git").exists():
                        hint = _repo_hint(p)
                        rows.append(f"- {p.name} — {p}" + (f" · {hint}" if hint else ""))
                    else:
                        rows.append(f"- {p.name} — {p} (not a git repo)")
                return ok("\n".join(rows) or "(no repositories found)")
            except OSError as e:
                return err(f"could not list {root}: {e}")

        @tool(
            "inspect_repo",
            "Look inside a repo BEFORE you brief a worker or judge their work: its "
            "guide docs (AGENTS.md / CLAUDE.md / README), top-level layout, and the "
            "likely check command. Manage as a technical lead who's read the code — "
            "you can also Read/Grep the repo directly at its path.",
            {"type": "object",
             "properties": {
                 "repo": {"type": "string",
                          "description": "repo name under the projects root, or absolute path"},
             },
             "required": ["repo"]},
        )
        async def inspect_repo(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._inspect_repo(str(args.get("repo", "")))

        @tool(
            "spawn_worker",
            "Hire a worker for one task. Creates a visible thread and an isolated "
            "git worktree of the repo, briefs the worker, and they start working. "
            "The brief must be self-contained (goal, constraints, definition of "
            "done). Returns the worker's id.",
            {"type": "object",
             "properties": {
                 "repo": {"type": "string",
                          "description": "repo name under the projects root, or absolute path"},
                 "task": {"type": "string", "description": "the self-contained brief"},
             },
             "required": ["repo", "task"]},
        )
        async def spawn_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._spawn_worker(str(args.get("repo", "")),
                                             str(args.get("task", "")))

        @tool(
            "message_worker",
            "Say something to a worker. Your message is posted in their thread "
            "(visible to the boss) and becomes the worker's next input.",
            {"type": "object",
             "properties": {
                 "worker_id": {"type": "string"},
                 "text": {"type": "string"},
             },
             "required": ["worker_id", "text"]},
        )
        async def message_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._message_worker(str(args.get("worker_id", "")),
                                               str(args.get("text", "")))

        @tool(
            "worker_status",
            "Current fleet state. Pass worker_id for one worker, omit for all.",
            {"type": "object",
             "properties": {"worker_id": {"type": "string"}}},
        )
        async def worker_status(args: dict[str, Any]) -> dict[str, Any]:
            rows = []
            for tid, rec in engine.store.workers().items():
                cid = rec.worker_id
                want = str(args.get("worker_id", "")).strip()
                if want and want != cid:
                    continue
                live = engine.sessions.get(tid)
                state = live.status if live else "dormant"
                rows.append(f"- {cid} ({rec.name}) [{state}] repo={rec.repo} "
                            f"status={rec.worker_status or 'working'} task={rec.task[:100]}")
            return ok("\n".join(rows) or "(no workers)")

        @tool(
            "dismiss_worker",
            "End a worker's engagement. Their worktree is removed if clean; a "
            "dirty worktree is preserved and its path reported.",
            {"type": "object",
             "properties": {"worker_id": {"type": "string"}},
             "required": ["worker_id"]},
        )
        async def dismiss_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._dismiss_worker(str(args.get("worker_id", "")))

        @tool(
            "review_worker",
            "Inspect a worker's committed work before delivery: returns the diff of "
            "their branch vs your checkout, whether everything is committed, and which "
            "delivery routes are available (a local 'merge' always; 'pr' if a remote + "
            "authenticated gh exist). Read-only — use it to surface the change to the boss.",
            {"type": "object",
             "properties": {"worker_id": {"type": "string"}},
             "required": ["worker_id"]},
        )
        async def review_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._review_worker(str(args.get("worker_id", "")))

        @tool(
            "run_checks",
            "Run a check command (tests / build / lint) INSIDE a worker's worktree "
            "and get back the REAL exit code + output — the way to VERIFY work "
            "actually passes instead of trusting the worker's word. e.g. "
            "command='uv run pytest' or 'npm test'. Do this before requesting delivery.",
            {"type": "object",
             "properties": {
                 "worker_id": {"type": "string"},
                 "command": {"type": "string",
                             "description": "the check command, e.g. 'uv run pytest'"},
             },
             "required": ["worker_id", "command"]},
        )
        async def run_checks(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._run_checks(str(args.get("worker_id", "")),
                                            str(args.get("command", "")))

        @tool(
            "deliver_worker",
            "REQUEST that a worker's finished work be landed. This does NOT land it — "
            "it asks the boss to approve, and the merge/PR runs only when they issue "
            "/approve. method='merge' (local merge into the branch the worker forked "
            "from) or 'pr' (push + open a GitHub PR). Call it once the work is "
            "committed and you've shown the boss the diff. Refuses uncommitted or "
            "empty branches.",
            {"type": "object",
             "properties": {
                 "worker_id": {"type": "string"},
                 "method": {"type": "string", "enum": ["merge", "pr"]},
             },
             "required": ["worker_id", "method"]},
        )
        async def deliver_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await engine._deliver_worker(str(args.get("worker_id", "")),
                                                str(args.get("method", "")))

        return create_sdk_mcp_server(
            "fleet",
            tools=[list_repos, inspect_repo, spawn_worker, message_worker,
                   worker_status, review_worker, run_checks, deliver_worker,
                   dismiss_worker],
        )

    # ---- fleet operations ------------------------------------------------

    def _find_worker(self, worker_id: str) -> tuple[str, ThreadRecord] | None:
        for tid, rec in self.store.workers().items():
            if rec.worker_id == worker_id:
                return tid, rec
        return None

    def _resolve_repo(self, repo_raw: str) -> Path | None:
        """A repo name under the projects root, or an absolute path → a real dir."""
        if not repo_raw.strip():
            return None
        repo = Path(repo_raw)
        if not repo.is_absolute():
            repo = self.settings.projects_root / repo_raw
        repo = repo.resolve()
        return repo if repo.is_dir() else None

    async def _inspect_repo(self, repo_raw: str) -> dict[str, Any]:
        """Ground the orchestrator in a repo: its guide docs, layout, check command —
        so it briefs and reviews from real knowledge of the code, not from the outside."""
        repo = self._resolve_repo(repo_raw)
        if repo is None:
            return {"content": [{"type": "text",
                    "text": f"no such repo: {repo_raw}"}], "is_error": True}
        parts = [f"# {repo.name} — {repo}"]
        for doc in ("AGENTS.md", "CLAUDE.md", "README.md"):
            text = _read_doc(repo / doc, limit=1800)
            if text:
                parts.append(f"\n## {doc}\n{text}")
        parts.append("\n## Top-level layout\n" + _top_level(repo))
        hint = _test_hint(repo)
        if hint:
            parts.append(f"\n## Likely check command\n`{hint}` — verify by actually "
                         f"running it with run_checks once a worker's on it.")
        body = "\n".join(parts)
        if len(body) > 6000:
            body = body[:6000] + "\n…(truncated — Read/Grep the repo directly for more)"
        return {"content": [{"type": "text", "text": body}]}

    async def _spawn_worker(self, repo_raw: str, task: str) -> dict[str, Any]:
        def err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        if not repo_raw.strip() or not task.strip():
            return err("repo and task are both required")
        repo = self._resolve_repo(repo_raw)
        if repo is None:
            return err(f"no such repo: {repo_raw}")

        taken = {r.worker_id for r in self.store.workers().values()}
        taken |= {r.name.lower() for r in self.store.workers().values()}
        name = pick_name(taken)
        worker_id = name.lower()

        # workspace: isolated worktree when the repo is git, else the repo itself.
        # Record the fork point (branch + SHA) so delivery lands on the right branch.
        base_branch, base_sha = "", ""
        try:
            if await worktrees.is_git_repo(repo):
                base_branch = await worktrees.current_branch(repo) or ""
                base_sha = await worktrees.head_sha(repo)
                wt = await worktrees.create_worktree(
                    repo, self.settings.state_dir / "worktrees", worker_id)
                cwd, isolated = wt, True
            else:
                cwd, isolated = repo, False
        except worktrees.WorktreeError as e:
            return err(f"couldn't set up an isolated workspace for {repo.name}: {e}")

        assert self.transport is not None
        thread_id = await self.transport.create_thread(
            f"{WORKER_EMOJI} {name} · {repo.name}")

        rec = ThreadRecord(
            role="worker", name=name, cwd=str(cwd), worker_id=worker_id,
            repo=str(repo), base_branch=base_branch, base_sha=base_sha,
            task=task.strip(), worker_status="working",
        )
        self.store.put(thread_id, rec)

        iso_note = ("isolated worktree, branch worker/" + worker_id if isolated
                    else "⚠️ not a git repo — working directly in the project dir")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=f"{name} hired for {repo.name} ({iso_note}).",
        ))
        # the orchestrator's brief, visible in the thread:
        await self._post(Outbound(
            thread_id=thread_id, speaker=self.orchestrator_speaker(), text=task.strip(),
        ))

        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return err("worker session failed to start (see thread)")
        await session.submit(
            f"[Brief from the orchestrator]\n{task.strip()}"
        )
        await self._refresh_dashboard()
        return {"content": [{"type": "text", "text":
                f"spawned worker {worker_id} ({name}) in {iso_note}; "
                f"thread created. They will report back via the fleet inbox."}]}

    async def _message_worker(self, worker_id: str, text: str) -> dict[str, Any]:
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip())
        if found is None:
            return err(f"no such worker: {worker_id}")
        if not text.strip():
            return err("text is required")
        thread_id, rec = found
        # being sent back to work un-sticks a done/blocked marker
        if rec.worker_status in ("done", "blocked"):
            self.store.update(thread_id, worker_status="working")
        await self._post(Outbound(
            thread_id=thread_id, speaker=self.orchestrator_speaker(), text=text.strip(),
        ))
        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return err("worker session unavailable")
        await session.submit(f"[From the orchestrator]: {text.strip()}")
        return {"content": [{"type": "text", "text": "delivered"}]}

    async def _dismiss_worker(self, worker_id: str) -> dict[str, Any]:
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip())
        if found is None:
            return err(f"no such worker: {worker_id}")
        thread_id, rec = found

        session = self.sessions.pop(thread_id, None)
        if session is not None:
            await session.stop()

        detail = ""
        wt = Path(rec.cwd)
        if rec.repo and wt != Path(rec.repo):
            removed, detail = await worktrees.remove_worktree(Path(rec.repo), wt)
            if not removed:
                detail = f" ({detail})"
            else:
                detail = ""

        self.store.update(thread_id, worker_status="dismissed")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=f"{rec.name} dismissed by the orchestrator.{detail}",
        ))
        if self.transport is not None:
            try:
                await self.transport.close_thread(thread_id)
            except Exception:  # noqa: BLE001
                pass
        await self._refresh_dashboard()
        return {"content": [{"type": "text", "text": f"dismissed {worker_id}{detail}"}]}

    async def _review_worker(self, worker_id: str) -> dict[str, Any]:
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip())
        if found is None:
            return err(f"no such worker: {worker_id}")
        _tid, rec = found
        if not rec.repo or rec.cwd == rec.repo:
            return err(f"{rec.name} isn't working in a git worktree — nothing to review or deliver")
        if not Path(rec.cwd).is_dir():
            return err(f"{rec.name}'s workspace is gone (was it dismissed?) — its work, "
                       f"if any, is on branch worker/{rec.worker_id}")
        repo = Path(rec.repo)
        branch = f"worker/{rec.worker_id}"
        base = rec.base_branch or await worktrees.current_branch(repo)
        if not base:
            return err(f"can't determine {rec.name}'s base branch (detached HEAD) — "
                       f"check out a branch in {repo.name}")
        committed = await worktrees.is_clean(Path(rec.cwd))
        has_work = await worktrees.branch_ahead(repo, base, branch)
        diff = await worktrees.branch_diff(repo, base, branch)
        routes = ["merge"]
        if await worktrees.has_remote(repo) and await worktrees.gh_available():
            routes.insert(0, "pr")
        commit_note = ("all changes committed" if committed else
                       "⚠️ uncommitted changes remain — have the worker commit first")
        work_note = "" if has_work else "\n- ⚠️ nothing committed on the branch yet"
        checks_line = {
            "pass": "✅ passed" if rec.checks_sha and rec.checks_sha == await worktrees.head_sha(Path(rec.cwd))
                    else "passed earlier, but the branch moved since (stale — re-run)",
            "fail": "❌ FAILED — must be fixed before this can be delivered",
        }.get(rec.checks, "not run yet — use run_checks to verify before delivering")
        return {"content": [{"type": "text", "text":
                f"Review of {rec.name} — branch {branch} vs {base}:\n"
                f"- {commit_note}\n"
                f"- checks: {checks_line}\n"
                f"- delivery routes available: {', '.join(routes)}{work_note}\n\n{diff}"}]}

    async def _run_checks(self, worker_id: str, command: str) -> dict[str, Any]:
        """Actually RUN a check command in the worker's worktree — real exit code,
        not the worker's word. Records the verdict + the revision it ran against so
        delivery can gate on it (and notice if the branch moved since)."""
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip())
        if found is None:
            return err(f"no such worker: {worker_id}")
        thread_id, rec = found
        command = command.strip()
        if not command:
            return err("a check command is required (e.g. 'uv run pytest' or 'npm test')")
        cwd = Path(rec.cwd)
        if not cwd.is_dir():
            return err(f"{rec.name}'s workspace is gone — nothing to check")
        code, output = await worktrees.run_command(cwd, command)
        sha = await worktrees.head_sha(cwd)  # the branch tip these checks ran against
        self.store.update(thread_id,
                          checks=("pass" if code == 0 else "fail"), checks_sha=sha)
        verdict = "✅ passed" if code == 0 else f"❌ FAILED (exit {code})"
        # Surface the REAL result into the worker's own thread so the boss sees proof.
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM,
            text=f"🧪 checks — `{command}` — {verdict}"))
        return {"content": [{"type": "text", "text":
                f"checks for {rec.name} — `{command}` — {verdict}\n\n{output}"}]}

    async def _deliver_worker(self, worker_id: str, method: str) -> dict[str, Any]:
        """The orchestrator REQUESTS delivery; it never lands work itself. The actual
        merge/PR runs only when a human issues /approve — a gate the LLM can't forge
        (an injected worker could otherwise talk the orchestrator into pushing)."""
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip())
        if found is None:
            return err(f"no such worker: {worker_id}")
        _thread_id, rec = found
        if method not in ("merge", "pr"):
            return err("method must be 'merge' or 'pr'")
        if rec.worker_status == "delivered":
            return err(f"{rec.name}'s work was already delivered")
        if not rec.repo or rec.cwd == rec.repo:
            return err(f"{rec.name} isn't in a git worktree — nothing to deliver")
        if not Path(rec.cwd).is_dir():
            return err(f"{rec.name}'s workspace is gone — nothing to deliver")
        if not await worktrees.is_clean(Path(rec.cwd)):
            return err(f"{rec.name} has uncommitted changes — have them commit first")
        repo = Path(rec.repo)
        base = rec.base_branch or await worktrees.current_branch(repo)
        if not base:
            return err(f"can't determine {rec.name}'s base branch — nothing to deliver into")
        if not await worktrees.branch_ahead(repo, base, f"worker/{rec.worker_id}"):
            return err(f"{rec.name} hasn't committed anything to deliver")
        # Verification gate: work whose checks last FAILED can't be delivered until
        # they're green again. (Not-yet-run is a soft warning, not a wall — the boss
        # decides, informed.)
        if rec.checks == "fail":
            return err(f"{rec.name}'s checks last FAILED — have them fix it and re-run "
                       f"run_checks until it's green before you can deliver.")
        tip = await worktrees.head_sha(Path(rec.cwd))
        if rec.checks == "pass" and rec.checks_sha == tip:
            checks_note = "\n✅ checks passed on this exact revision"
        elif rec.checks == "pass":
            checks_note = ("\n⚠️ checks passed earlier, but there are new commits since — "
                           "consider run_checks again before approving")
        else:
            checks_note = "\n⚠️ no checks recorded — consider run_checks first to verify"
        # Hard gate: record the request and ask the boss to confirm with a command.
        self._pending_delivery[rec.worker_id] = method
        verb = "open a pull request for" if method == "pr" else "locally merge"
        await self._post(Outbound(
            thread_id=self._last_boss_thread, speaker=SYSTEM,
            text=(f"🚦 {rec.name} is ready to deliver. Approve to {verb} their work "
                  f"into '{base}'?{checks_note}\n"
                  f"    /approve {rec.worker_id}    ·    /reject {rec.worker_id}")))
        await self._refresh_dashboard()
        return {"content": [{"type": "text", "text":
                f"delivery of {rec.name} via {method} requested — the boss must "
                f"/approve {rec.worker_id} to authorize it (I can't land it myself)."}]}

    async def approve_delivery(self, worker_id: str) -> str:
        """The hard gate: called only by an allowlisted human's /approve, so an
        injected orchestrator can't self-authorize a push/merge."""
        wid = worker_id.strip().lower()
        method = self._pending_delivery.pop(wid, None)
        if method is None:
            return f"No pending delivery for '{wid}'."
        return await self._execute_delivery(wid, method)

    async def reject_delivery(self, worker_id: str) -> str:
        wid = worker_id.strip().lower()
        method = self._pending_delivery.pop(wid, None)
        if method is None:
            return f"No pending delivery for '{wid}'."
        found = self._find_worker(wid)
        name = found[1].name if found else wid
        self._note(f"the boss rejected delivery of {name}")
        await self._refresh_dashboard()
        await self._wake_orchestrator()
        return f"Rejected {name}'s delivery; the orchestrator has been told."

    async def _execute_delivery(self, worker_id: str, method: str) -> str:
        found = self._find_worker(worker_id)
        if found is None:
            return f"Worker '{worker_id}' is gone; nothing delivered."
        thread_id, rec = found
        repo = Path(rec.repo)
        branch = f"worker/{rec.worker_id}"
        base = rec.base_branch or await worktrees.current_branch(repo) or ""
        if method == "pr":
            landed, detail = await worktrees.open_pr(repo, branch, base)
        else:
            landed, detail = await worktrees.merge_into_base(repo, branch, base)
        if not landed:
            self._note(f"delivery of {rec.name} failed: {detail}")
            await self._wake_orchestrator()
            return f"⚠️ delivery of {rec.name} failed: {detail}"
        self.store.update(thread_id, worker_status="delivered")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM, text=f"📦 {detail}."))
        self._note(f"{rec.name}'s work delivered — {detail}")
        await self._refresh_dashboard()
        await self._wake_orchestrator()
        return f"✅ {rec.name}'s work delivered — {detail}"

    # ---- direct sessions (orchestrator-less, via /new) -------------------

    async def new_direct(self, path_raw: str, name: str | None) -> tuple[str, str] | str:
        """Create a direct session thread. Returns (thread_id, name) or error str."""
        p = Path(path_raw).expanduser()
        if not p.is_absolute():
            p = self.settings.projects_root / path_raw
        p = p.resolve()
        if not p.is_dir():
            return f"❌ Not a directory: {p}"
        title = name or p.name
        assert self.transport is not None
        thread_id = await self.transport.create_thread(title)
        rec = ThreadRecord(role="direct", name=title, cwd=str(p))
        self.store.put(thread_id, rec)
        session = await self._ensure_session(thread_id, rec)
        if session is None:
            return "❌ session failed to start"
        return thread_id, title

    async def interrupt(self, thread_id: str) -> bool:
        # /stop in a DM should stop the orchestrator — its session lives under the
        # main thread, not the DM's id.
        if self._is_orchestrator_thread(thread_id):
            thread_id = self.main_thread
        session = self.sessions.get(thread_id)
        if session is None:
            return False
        await session.interrupt()
        return True

    async def kill(self, thread_id: str) -> bool:
        rec = self.store.get(thread_id)
        session = self.sessions.pop(thread_id, None)
        if session is not None:
            await session.stop()
        if rec is None:
            return session is not None
        if rec.role == "worker" and rec.repo and rec.cwd != rec.repo:
            await worktrees.remove_worktree(Path(rec.repo), Path(rec.cwd))
        if rec.role == "orchestrator":
            self.store.set_orchestrator_thread(None)
        self.store.delete(thread_id)
        if rec.role == "worker":
            await self._refresh_dashboard()
        return True

    def listing(self) -> list[tuple[str, ThreadRecord, str]]:
        rows = []
        for tid, rec in self.store.all().items():
            live = self.sessions.get(tid)
            rows.append((tid, rec, live.status if live else "dormant"))
        return rows

    def rehydrate(self) -> None:
        """After a restart, re-surface workers still awaiting the orchestrator.

        The supervision inbox is in-memory and does not survive a restart, so an
        unfinished worker (finished-but-not-landed, or blocked) could otherwise be
        silently forgotten. Re-enqueue a reminder; it's delivered on the
        orchestrator's next wake (a boss message or a live worker event).
        """
        pending = [rec for rec in self.store.workers().values()
                   if rec.worker_status in ("blocked", "done")]
        if pending:
            names = ", ".join(f"{r.name} ({r.worker_status})" for r in pending)
            self._note(f"[after restart] workers still awaiting you: {names}")

    async def factory_reset(self) -> str:
        """Wipe everything the bot knows: live sessions, all conversation memory
        (session histories), fleet records, worktrees, worker topics, and the
        dashboard. The next message starts from a true blank slate — a fresh
        implementation sees zero old context. Only reachable via a human's
        explicit `/reset confirm`."""
        for session in list(self.sessions.values()):
            try:
                await session.stop()
            except Exception:  # noqa: BLE001
                pass
        self.sessions.clear()
        self._inbox.clear()
        self._waking = False
        self._pending_delivery.clear()
        self._last_dashboard = ""
        self._last_boss_thread = self.main_thread

        delete_thread = getattr(self.transport, "delete_thread", None)
        for tid, rec in list(self.store.all().items()):
            if rec.role == "worker" and rec.repo and rec.cwd and rec.cwd != rec.repo:
                try:  # factory reset discards work in progress, dirty or not
                    await worktrees.force_remove_worktree(Path(rec.repo), Path(rec.cwd))
                except Exception:  # noqa: BLE001
                    pass
            if delete_thread is not None:
                try:  # worker/direct topics vanish, messages and all
                    await delete_thread(tid)
                except Exception:  # noqa: BLE001
                    pass
        delete_dashboard = getattr(self.transport, "delete_dashboard", None)
        if delete_dashboard is not None:
            try:
                await delete_dashboard()
            except Exception:  # noqa: BLE001
                pass

        self.store.wipe()
        for sub in ("orchestrator-home", "worktrees"):
            shutil.rmtree(self.settings.state_dir / sub, ignore_errors=True)
        log.warning("factory reset executed — all state wiped")
        return ("🏭 Factory reset complete — blank slate. Committed worker/* "
                "branches in your repos were left untouched.")

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await session.stop()
        self.sessions.clear()
