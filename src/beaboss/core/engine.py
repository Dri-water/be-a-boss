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
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, create_sdk_mcp_server, tool

from .agent_backend import CodexBackend
from .names import pick_name
from .ports import InboundMessage, MediaIn, Outbound, Speaker, SYSTEM, Transport
from .session import CoreSession
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
    "You are the ORCHESTRATOR of a small software organisation, talking with your "
    "boss in a chat thread. You do not write project code yourself — you run a "
    "team of worker agents and use your fleet tools to get work done:\n"
    "- mcp__fleet__list_repos() — see the available repositories\n"
    "- mcp__fleet__spawn_worker(repo, task) — hire a worker: creates a visible "
    "thread and an isolated git worktree, briefs them, and they start working\n"
    "- mcp__fleet__message_worker(worker_id, text) — speak to a worker (your message "
    "is visible in their thread)\n"
    "- mcp__fleet__worker_status(worker_id?) — current fleet state\n"
    "- mcp__fleet__review_worker(worker_id) — inspect a worker's committed diff and "
    "which delivery routes are available; use it to show the boss the change\n"
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
    "evidence.\n\n"
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
    "then show the boss the actual change (a short summary of what changed plus the "
    "key parts of the diff) with your recommendation.\n"
    "- Only after the boss's explicit go-ahead, call deliver_worker. Prefer 'pr' when "
    "it's available (a remote + gh) so the change stays reviewable; otherwise 'merge' "
    "for a local land. The merge is deterministic and refuses a dirty checkout — if it "
    "refuses, relay the reason plainly, never force it.\n"
    "- After a clean delivery you may dismiss the worker.\n\n"
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


class Engine:
    def __init__(self, settings, store: CoreStore):
        self.settings = settings
        self.store = store
        self.transport: Transport | None = None
        self.sessions: dict[str, CoreSession] = {}
        self._inbox: list[str] = []          # pending notes for the orchestrator
        self._waking = False                 # digest wake in flight
        self._session_locks: dict[str, asyncio.Lock] = {}  # per-thread start lock
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

    # ---- inbound routing -------------------------------------------------

    async def on_inbound(self, msg: InboundMessage) -> None:
        rec = self.store.get(msg.thread_id)

        if rec is None and msg.thread_id == self.main_thread:
            # First contact in the office thread: bring the orchestrator to life.
            await self._ensure_orchestrator(msg.thread_id)
            rec = self.store.get(msg.thread_id)

        if rec is None:
            await self._post(Outbound(
                thread_id=msg.thread_id, speaker=SYSTEM,
                text="This thread isn't active. Talk to the orchestrator in the "
                     "main thread, or use /new for a direct session.",
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

        # orchestrator or direct: plain turn
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
        )

    def _make_worker_session(self, thread_id: str, rec: ThreadRecord) -> CoreSession:
        cwd = Path(rec.cwd)
        # A worker may run on the Codex CLI instead of Claude; the choice lives
        # here (the single worker-construction point) and nowhere else.
        backend = (
            CodexBackend(cwd)
            if self.settings.agent_backend == "codex"
            else None  # None => CoreSession builds the default ClaudeAgentBackend
        )
        session = CoreSession(
            thread_id=thread_id, cwd=cwd,
            speaker=self.worker_speaker(rec.name),
            settings=self.settings, post=self._post, busy=self._busy,
            on_session_id=self._sid_saver(thread_id), session_id=rec.session_id,
            system_append=None,  # default env note…
            backend=backend,
        )
        # …plus the worker role note appended onto it
        session._system_append = session._resolve_append() + WORKER_APPEND_EXTRA
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

    async def _on_worker_turn_done(self, session: CoreSession, result: ResultMessage) -> None:
        rec = self.store.get(session.thread_id)
        if rec is None:
            return
        tail = (result.result or "").strip()
        if len(tail) > 600:
            tail = tail[:600] + "…"
        # track the worker's self-reported status line if present
        low = tail.lower()
        if "status: done" in low:
            self.store.update(session.thread_id, worker_status="done")
        elif "status: blocked" in low or "status: needs-decision" in low:
            self.store.update(session.thread_id, worker_status="blocked")
        status = "errored" if result.is_error else "finished a turn"
        self._note(f"worker {rec.worker_id} ({rec.name}, task: {rec.task[:80]}) "
                   f"{status}: {tail or '(no text)'}")
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
                await session.submit(digest)
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
            "List the repositories available under the projects root.",
            {"type": "object", "properties": {}},
        )
        async def list_repos(args: dict[str, Any]) -> dict[str, Any]:
            root = engine.settings.projects_root
            try:
                rows = []
                for p in sorted(root.iterdir()):
                    if p.is_dir() and not p.name.startswith("."):
                        tag = " (git)" if (p / ".git").exists() else ""
                        rows.append(f"- {p.name}{tag}")
                return ok("\n".join(rows) or "(no repositories found)")
            except OSError as e:
                return err(f"could not list {root}: {e}")

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
            "deliver_worker",
            "Land a worker's work — ONLY after the boss has explicitly approved. "
            "method='merge' deterministically merges the worker's branch into your "
            "checkout's current branch (requires it clean; aborts on conflict, never "
            "force-anything). method='pr' pushes the branch and opens a GitHub PR "
            "(requires a remote + authenticated gh). Refuses uncommitted work.",
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
            tools=[list_repos, spawn_worker, message_worker, worker_status,
                   review_worker, deliver_worker, dismiss_worker],
        )

    # ---- fleet operations ------------------------------------------------

    def _find_worker(self, worker_id: str) -> tuple[str, ThreadRecord] | None:
        for tid, rec in self.store.workers().items():
            if rec.worker_id == worker_id:
                return tid, rec
        return None

    async def _spawn_worker(self, repo_raw: str, task: str) -> dict[str, Any]:
        def err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        if not repo_raw.strip() or not task.strip():
            return err("repo and task are both required")
        repo = Path(repo_raw)
        if not repo.is_absolute():
            repo = self.settings.projects_root / repo_raw
        repo = repo.resolve()
        if not repo.is_dir():
            return err(f"no such repo: {repo}")

        taken = {r.worker_id for r in self.store.workers().values()}
        taken |= {r.name.lower() for r in self.store.workers().values()}
        name = pick_name(taken)
        worker_id = name.lower()

        # workspace: isolated worktree when the repo is git, else the repo itself
        try:
            if await worktrees.is_git_repo(repo):
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
            repo=str(repo), task=task.strip(), worker_status="working",
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
        repo = Path(rec.repo)
        branch = f"worker/{rec.worker_id}"
        base = await worktrees.current_branch(repo) or "the base branch"
        committed = await worktrees.is_clean(Path(rec.cwd))
        diff = await worktrees.branch_diff(repo, base, branch)
        routes = ["merge"]
        if await worktrees.has_remote(repo) and await worktrees.gh_available():
            routes.insert(0, "pr")
        commit_note = ("all changes committed" if committed else
                       "⚠️ uncommitted changes remain — have the worker commit before delivery")
        return {"content": [{"type": "text", "text":
                f"Review of {rec.name} — branch {branch} vs {base}:\n"
                f"- {commit_note}\n"
                f"- delivery routes available: {', '.join(routes)}\n\n{diff}"}]}

    async def _deliver_worker(self, worker_id: str, method: str) -> dict[str, Any]:
        def err(t: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "is_error": True}

        found = self._find_worker(worker_id.strip())
        if found is None:
            return err(f"no such worker: {worker_id}")
        thread_id, rec = found
        if not rec.repo or rec.cwd == rec.repo:
            return err(f"{rec.name} isn't in a git worktree — nothing to deliver")
        if not await worktrees.is_clean(Path(rec.cwd)):
            return err(f"{rec.name} has uncommitted changes — have them commit before delivery")
        branch = f"worker/{rec.worker_id}"
        repo = Path(rec.repo)
        if method == "pr":
            landed, detail = await worktrees.open_pr(repo, branch)
        elif method == "merge":
            landed, detail = await worktrees.merge_into_current(repo, branch)
        else:
            return err("method must be 'merge' or 'pr'")
        if not landed:
            return err(f"delivery failed: {detail}")
        self.store.update(thread_id, worker_status="delivered")
        await self._post(Outbound(
            thread_id=thread_id, speaker=SYSTEM, text=f"📦 {detail}."))
        return {"content": [{"type": "text", "text": f"{rec.name}'s work delivered — {detail}"}]}

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
        return True

    def listing(self) -> list[tuple[str, ThreadRecord, str]]:
        rows = []
        for tid, rec in self.store.all().items():
            live = self.sessions.get(tid)
            rows.append((tid, rec, live.status if live else "dormant"))
        return rows

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await session.stop()
        self.sessions.clear()
