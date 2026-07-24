import asyncio
import subprocess
from pathlib import Path

from beaboss.config import Settings
from beaboss.core import worktrees
from beaboss.core.engine import Engine
from beaboss.core.ports import InboundMessage, Outbound
from beaboss.core.store import CoreStore, ThreadRecord


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _repo(tmp: Path, name: str) -> Path:
    repo = tmp / "projects" / name
    repo.mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _settings(tmp: Path) -> Settings:
    return Settings(
        bot_token="t", allowed_user_ids={1}, chat_id=None,
        permission_mode="bypassPermissions", projects_root=tmp / "projects",
        cli_path=None, model=None, max_turns=None, state_dir=tmp / "state",
        bot_name="Lim Wei Jie", session_system_append=None,
    )


class FakeTransport:
    def __init__(self):
        self.posts: list[Outbound] = []
        self.threads: list[str] = []
        self.closed: list[str] = []
        self._next = 100

    async def create_thread(self, title: str) -> str:
        self._next += 1
        tid = str(self._next)
        self.threads.append(title)
        return tid

    async def close_thread(self, thread_id):
        self.closed.append(thread_id)

    async def post(self, out: Outbound):
        self.posts.append(out)

    async def indicate_busy(self, thread_id):
        pass


class FakeSession:
    """Stands in for CoreSession — records submits, no Claude involved."""

    def __init__(self):
        self.submitted: list[str] = []
        self.reply_tos: list[str | None] = []
        self.media: list[tuple[str, int]] = []
        self.status = "idle"
        self.pending = 0
        self.alive = True

    async def submit(self, text, reply_to=None, quiet_ok=False):
        self.submitted.append(text)
        self.reply_tos.append(reply_to)

    async def submit_media(self, caption, items, reply_to=None):
        self.media.append((caption, len(items)))
        self.reply_tos.append(reply_to)

    async def stop(self):
        self.status = "stopped"

    async def interrupt(self):
        pass


def _engine(tmp: Path) -> tuple[Engine, FakeTransport]:
    (tmp / "projects").mkdir(parents=True, exist_ok=True)
    engine = Engine(_settings(tmp), CoreStore(tmp / "state"))
    engine.WAKE_COALESCE_SECS = 0  # no waiting in tests
    t = FakeTransport()
    engine.attach_transport(t)
    return engine, t


def test_unknown_thread_gets_system_hint(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.set_orchestrator_thread("general")  # office exists elsewhere
    asyncio.run(engine.on_inbound(InboundMessage(thread_id="999", text="hi")))
    assert len(t.posts) == 1 and t.posts[0].speaker.role == "system"


def test_first_message_only_bootstraps_office_in_main_thread(tmp_path):
    """A first message to a random (non-main) thread must NOT claim the
    orchestrator's office — only the main thread does."""
    engine, t = _engine(tmp_path)
    assert engine.store.orchestrator_thread is None
    asyncio.run(engine.on_inbound(InboundMessage(thread_id="777", text="hi")))
    assert engine.store.orchestrator_thread is None  # office not claimed
    assert t.posts and t.posts[-1].speaker.role == "system"  # got the hint


def test_interjection_reaches_worker_and_inbox(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", cwd=str(tmp_path), worker_id="nova",
        repo=str(tmp_path), task="fix"))
    fake = FakeSession()
    engine.sessions["55"] = fake

    asyncio.run(engine.on_inbound(InboundMessage(
        thread_id="55", text="check the TTL too", sender_name="Jon")))

    assert len(fake.submitted) == 1
    assert "Interjection from Jon" in fake.submitted[0]
    assert "check the TTL too" in fake.submitted[0]
    assert any("Jon said in nova's thread" in n for n in engine._inbox)


def test_orchestrator_wake_digest_drains_inbox(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    engine.store.set_orchestrator_thread("general")
    fake = FakeSession()
    engine.sessions["general"] = fake

    engine._note("worker nova finished: STATUS: done")
    engine._note("worker kite blocked: STATUS: blocked: need creds")
    asyncio.run(engine._wake_orchestrator())

    assert len(fake.submitted) == 1
    digest = fake.submitted[0]
    assert digest.startswith("[fleet inbox]")
    assert "nova finished" in digest and "kite blocked" in digest
    assert engine._inbox == []


def test_wake_drains_notes_arriving_during_digest(tmp_path):
    """Regression: a worker finishing while the orchestrator is mid-digest must not
    be stranded — the wake loop must pick up the late note and deliver it too."""
    engine, t = _engine(tmp_path)
    engine.store.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    engine.store.set_orchestrator_thread("general")

    class LateNoteSession:
        def __init__(self):
            self.digests = []
            self.status = "idle"
            self.pending = 0
            self.alive = True

        async def submit(self, text, reply_to=None, quiet_ok=False):
            self.digests.append(text)
            if len(self.digests) == 1:  # a worker finishes "during" the first digest
                engine._note("worker kite finished late")

        async def stop(self):
            pass

    fake = LateNoteSession()
    engine.sessions["general"] = fake

    engine._note("worker nova finished")
    asyncio.run(engine._wake_orchestrator())

    assert len(fake.digests) == 2, fake.digests
    assert "nova" in fake.digests[0] and "kite" in fake.digests[1]
    assert engine._inbox == []


def test_orchestrator_message_routes_plain(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    engine.store.set_orchestrator_thread("general")
    fake = FakeSession()
    engine.sessions["general"] = fake
    asyncio.run(engine.on_inbound(InboundMessage(thread_id="general", text="status?")))
    assert len(fake.submitted) == 1
    # every boss turn is grounded in code-generated fleet truth
    assert fake.submitted[0].startswith("[fleet right now: no workers exist]")
    assert fake.submitted[0].endswith("status?")


def test_spawn_worker_worktree_failure_is_clean(tmp_path, monkeypatch):
    """If worktree setup fails during spawn, the tool returns a clean error and
    creates no thread — no raw traceback leaks to the orchestrator."""
    engine, t = _engine(tmp_path)
    repo = tmp_path / "projects" / "myrepo"
    repo.mkdir(parents=True)

    async def fake_is_git(path):
        return True

    async def fake_create(*args, **kwargs):
        raise worktrees.WorktreeError("branch 'worker/nova' already exists — retry")

    monkeypatch.setattr(worktrees, "is_git_repo", fake_is_git)
    monkeypatch.setattr(worktrees, "create_worktree", fake_create)

    res = asyncio.run(engine._spawn_worker("myrepo", "do a task"))

    assert res.get("is_error") is True
    text = res["content"][0]["text"]
    assert "isolated workspace" in text
    assert "worker/nova" in text  # surfaces the underlying reason
    assert t.threads == []       # nothing half-created


def test_rehydrate_resurfaces_pending_workers(tmp_path):
    """After a restart, blocked / finished-but-not-landed workers are re-surfaced to
    the orchestrator; terminal (dismissed) ones are not."""
    engine, t = _engine(tmp_path)
    engine.store.put("9", ThreadRecord(role="worker", name="Nova", worker_id="nova",
                                       worker_status="blocked"))
    engine.store.put("10", ThreadRecord(role="worker", name="Kite", worker_id="kite",
                                        worker_status="done"))
    engine.store.put("11", ThreadRecord(role="worker", name="Ada", worker_id="ada",
                                        worker_status="dismissed"))
    engine.rehydrate()
    assert len(engine._inbox) == 1
    note = engine._inbox[0]
    assert "Nova" in note and "Kite" in note and "Ada" not in note


def test_dm_message_routes_to_one_orchestrator_replying_in_the_dm(tmp_path):
    """A DM drives the single orchestrator (thread 'general') and its reply is
    targeted back to the DM — one brain, no separate office."""
    engine, t = _engine(tmp_path)
    engine.store.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    engine.store.set_orchestrator_thread("general")
    fake = FakeSession()
    engine.sessions["general"] = fake

    asyncio.run(engine.on_inbound(InboundMessage(thread_id="dm:42", text="change the button")))

    # went to the ONE orchestrator session, carrying the DM as its reply target,
    # grounded with the live fleet snapshot
    assert len(fake.submitted) == 1
    assert "[fleet right now:" in fake.submitted[0]
    assert fake.submitted[0].endswith("change the button")
    assert fake.reply_tos == ["dm:42"]


def test_status_parsed_from_full_reply_not_truncated_digest(tmp_path):
    """Regression: a worker's STATUS line lives on the LAST line of a long reply —
    it must be parsed before digest truncation, or workers freeze at 'working'."""
    engine, t = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", cwd=str(tmp_path), worker_id="nova",
        repo=str(tmp_path), task="x", worker_status="working"))

    class LongDone:
        result = ("summary line\n" * 200) + "STATUS: done"   # way past 600 chars
        is_error = False

    sess = FakeSession()
    sess.thread_id = "55"
    asyncio.run(engine._on_worker_turn_done(sess, LongDone()))
    assert engine.store.get("55").worker_status == "done"


def test_message_worker_unsticks_done_status(tmp_path):
    """Sending a done/blocked worker back to work resets it to 'working' so the
    fleet snapshot and dashboard tell the truth."""
    engine, t = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", cwd=str(tmp_path), worker_id="nova",
        repo=str(tmp_path), task="x", worker_status="done"))
    engine.sessions["55"] = FakeSession()
    asyncio.run(engine._message_worker("nova", "checks failed — fix them"))
    assert engine.store.get("55").worker_status == "working"


def test_digest_replies_follow_the_boss(tmp_path):
    """A worker finishing a DM-initiated task reports back into that DM, not into a
    silent #general — the digest turn carries the boss's last thread as reply_to."""
    engine, t = _engine(tmp_path)
    engine.store.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    engine.store.set_orchestrator_thread("general")
    fake = FakeSession()
    engine.sessions["general"] = fake

    # boss last spoke from a DM
    asyncio.run(engine.on_inbound(InboundMessage(thread_id="dm:42", text="build it")))
    # a worker event wakes the orchestrator
    engine._note("worker nova finished: STATUS: done")
    asyncio.run(engine._wake_orchestrator())

    assert fake.reply_tos[-1] == "dm:42"      # digest replies land in the DM


def test_factory_reset_wipes_everything(tmp_path):
    """/reset confirm → blank slate: sessions stopped, records gone, dirty
    worktrees force-removed, pending approvals cleared."""
    engine, t = _engine(tmp_path)
    repo = _repo(tmp_path, "resetme")
    dest = asyncio.run(worktrees.create_worktree(
        repo, tmp_path / "state" / "worktrees", "nova"))
    (dest / "dirty.txt").write_text("uncommitted\n")   # dirty → normal removal refuses
    engine.store.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    engine.store.set_orchestrator_thread("general")
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", worker_id="nova",
        cwd=str(dest), repo=str(repo), task="x"))
    engine.sessions["general"] = FakeSession()
    engine._note("stale note")
    engine._pending_delivery["nova"] = "merge"

    result = asyncio.run(engine.factory_reset())
    assert "Factory reset complete" in result
    assert engine.store.all() == {}
    assert engine.store.orchestrator_thread is None
    assert engine.sessions == {}
    assert engine._inbox == [] and engine._pending_delivery == {}
    assert not dest.exists()                   # dirty worktree force-removed


def test_dismissed_worker_is_not_resummoned(tmp_path):
    """A message to a dismissed worker (worktree gone) must not rebuild a session
    into a torn-down workspace — it gets a clean explanation instead."""
    engine, t = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", cwd=str(tmp_path), worker_id="nova",
        repo=str(tmp_path), task="x", worker_status="dismissed"))
    asyncio.run(engine.on_inbound(InboundMessage(thread_id="55", text="you there?")))
    assert "55" not in engine.sessions                       # no session created
    assert any("dismissed" in p.text for p in t.posts)


def test_dashboard_buckets_the_fleet(tmp_path):
    """The #general board shows the fleet bucketed by state, rendered from the store."""
    engine, _ = _engine(tmp_path)
    engine.store.put("1", ThreadRecord(
        role="worker", name="Nova", worker_id="nova", repo="/r/app",
        worker_status="working", task="build X"))
    engine.store.put("2", ThreadRecord(
        role="worker", name="Kite", worker_id="kite", repo="/r/app",
        worker_status="blocked", task="fix Y"))
    engine._pending_delivery["nova"] = "merge"

    board = engine._render_dashboard()
    assert "Kite" in board and "Blocked" in board   # blocked shown
    assert "/approve nova" in board                  # nova awaiting approval


def test_inspect_repo_grounds_the_orchestrator(tmp_path):
    """inspect_repo returns the repo's real guide docs, layout, and a detected check
    command — so the orchestrator briefs/reviews from knowledge, not the outside."""
    engine, _ = _engine(tmp_path)
    repo = _repo(tmp_path, "myapp")
    (repo / "AGENTS.md").write_text("# myapp\nA widget service. Run tests with pytest.\n")
    (repo / "pyproject.toml").write_text("[project]\nname='myapp'\n")
    (repo / "src").mkdir()

    res = asyncio.run(engine._inspect_repo("myapp"))
    text = res["content"][0]["text"]
    assert res.get("is_error") is not True
    assert "A widget service" in text          # read the guide doc
    assert "src/" in text                        # saw the layout
    assert "uv run pytest" in text               # detected the check command


def test_inspect_repo_unknown_is_clean_error(tmp_path):
    engine, _ = _engine(tmp_path)
    res = asyncio.run(engine._inspect_repo("nope"))
    assert res.get("is_error") is True
    assert "no such repo" in res["content"][0]["text"]


def test_speakers(tmp_path):
    engine, _ = _engine(tmp_path)
    o = engine.orchestrator_speaker()
    assert o.role == "orchestrator" and o.name == "Lim Wei Jie" and o.emoji == "🧭"
    c = engine.worker_speaker("Nova")
    assert c.label == "⚙️ Nova"


def test_listing_and_kill_direct(tmp_path):
    engine, t = _engine(tmp_path)
    engine.store.put("7", ThreadRecord(role="direct", name="d", cwd=str(tmp_path)))
    rows = engine.listing()
    assert rows[0][2] == "dormant"
    assert asyncio.run(engine.kill("7")) is True
    assert engine.store.get("7") is None


def test_deliver_requests_approval_then_human_lands_it(tmp_path):
    """CONSERVATIVE mode: deliver_worker only REQUESTS; nothing lands until a human
    /approve. review surfaces the diff; approve does the merge."""
    engine, t = _engine(tmp_path)
    engine.settings.deploy_braveness = "conservative"
    repo = _repo(tmp_path, "myrepo")
    dest = asyncio.run(worktrees.create_worktree(repo, tmp_path / "state" / "worktrees", "nova"))
    (dest / "feature.py").write_text("x = 1\n")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-m", "feat")
    base = asyncio.run(worktrees.current_branch(repo))
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", cwd=str(dest), worker_id="nova",
        repo=str(repo), base_branch=base, task="add feature"))

    review = asyncio.run(engine._review_worker("nova"))["content"][0]["text"]
    assert "feature.py" in review and "merge" in review

    # deliver only REQUESTS — it must NOT land, and must post an approval prompt
    req = asyncio.run(engine._deliver_worker("nova", "merge"))
    assert req.get("is_error") is not True
    assert "approve" in req["content"][0]["text"].lower()
    assert not (repo / "feature.py").exists()          # nothing landed yet
    assert any("🚦" in p.text for p in t.posts)
    assert engine._pending_delivery.get("nova") == "merge"

    # the human's /approve is the gate that actually lands it
    result = asyncio.run(engine.approve_delivery("nova"))
    assert "delivered" in result
    assert (repo / "feature.py").exists()               # now landed
    assert engine.store.get("55").worker_status == "delivered"
    assert "nova" not in engine._pending_delivery


def test_deliver_refuses_uncommitted_work(tmp_path):
    engine, t = _engine(tmp_path)
    repo = _repo(tmp_path, "r2")
    dest = asyncio.run(worktrees.create_worktree(repo, tmp_path / "state" / "worktrees", "kite"))
    (dest / "wip.py").write_text("unfinished\n")  # uncommitted
    base = asyncio.run(worktrees.current_branch(repo))
    engine.store.put("56", ThreadRecord(
        role="worker", name="Kite", cwd=str(dest), worker_id="kite",
        repo=str(repo), base_branch=base, task="x"))

    res = asyncio.run(engine._deliver_worker("kite", "merge"))
    assert res.get("is_error") is True
    assert "uncommitted" in res["content"][0]["text"]
    assert "kite" not in engine._pending_delivery  # not queued for approval


def test_approve_without_request_is_a_noop(tmp_path):
    engine, t = _engine(tmp_path)
    result = asyncio.run(engine.approve_delivery("ghost"))
    assert "No pending delivery" in result


def _worker_with_commit(engine, tmp_path, repo_name, wid):
    """A worker whose branch has one real commit — the common setup for the
    verification tests below."""
    repo = _repo(tmp_path, repo_name)
    dest = asyncio.run(worktrees.create_worktree(
        repo, tmp_path / "state" / "worktrees", wid))
    (dest / "feature.py").write_text("x = 1\n")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-m", "feat")
    base = asyncio.run(worktrees.current_branch(repo))
    engine.store.put("55", ThreadRecord(
        role="worker", name=wid.title(), cwd=str(dest), worker_id=wid,
        repo=str(repo), base_branch=base, task="x"))
    return repo, dest


def test_run_checks_records_real_pass_and_fail(tmp_path):
    """run_checks runs the command for real and records the true verdict against the
    branch tip — pass and fail both, not the worker's say-so."""
    engine, t = _engine(tmp_path)
    _worker_with_commit(engine, tmp_path, "checkrepo", "nova")

    ok = asyncio.run(engine._run_checks("nova", "python -c \"exit(0)\""))
    assert ok.get("is_error") is not True
    assert "✅" in ok["content"][0]["text"]
    assert engine.store.get("55").checks == "pass"
    assert engine.store.get("55").checks_sha  # recorded the revision it ran against
    assert any("🧪" in p.text and "✅" in p.text for p in t.posts)  # proof in-thread

    bad = asyncio.run(engine._run_checks("nova", "python -c \"exit(1)\""))
    assert "FAILED" in bad["content"][0]["text"]
    assert engine.store.get("55").checks == "fail"


def test_failed_checks_block_delivery(tmp_path):
    """The teeth: a worker whose checks last failed cannot be delivered — the request
    is refused and nothing is queued for approval."""
    engine, t = _engine(tmp_path)
    _worker_with_commit(engine, tmp_path, "gaterepo", "kite")

    asyncio.run(engine._run_checks("kite", "python -c \"exit(1)\""))
    res = asyncio.run(engine._deliver_worker("kite", "merge"))
    assert res.get("is_error") is True
    assert "FAILED" in res["content"][0]["text"]
    assert "kite" not in engine._pending_delivery   # not queued for /approve


def test_passing_checks_surface_in_approval_prompt(tmp_path):
    """CONSERVATIVE: green checks on the delivered revision are shown in the approval
    request so the boss approves with the real result in hand."""
    engine, t = _engine(tmp_path)
    engine.settings.deploy_braveness = "conservative"
    _worker_with_commit(engine, tmp_path, "greenrepo", "ada")

    asyncio.run(engine._run_checks("ada", "python -c \"exit(0)\""))
    req = asyncio.run(engine._deliver_worker("ada", "merge"))
    assert req.get("is_error") is not True
    assert engine._pending_delivery.get("ada") == "merge"
    assert any("🚦" in p.text and "✅ checks passed" in p.text for p in t.posts)


def test_balanced_mode_lands_directly(tmp_path):
    """BALANCED (default): the orchestrator lands work directly — no 🚦, no pending —
    on the boss's word. The checks gate and safety guards still apply."""
    engine, t = _engine(tmp_path)
    assert engine.settings.deploy_braveness == "balanced"   # the default
    repo = _repo(tmp_path, "gf")
    dest = asyncio.run(worktrees.create_worktree(
        repo, tmp_path / "state" / "worktrees", "nova"))
    (dest / "feature.py").write_text("x = 1\n")
    _git(dest, "add", "-A"); _git(dest, "commit", "-m", "feat")
    base = asyncio.run(worktrees.current_branch(repo))
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", cwd=str(dest), worker_id="nova",
        repo=str(repo), base_branch=base, task="x"))

    res = asyncio.run(engine._deliver_worker("nova", "merge"))
    assert res.get("is_error") is not True
    assert "delivered" in res["content"][0]["text"]          # landed, not "requested"
    assert (repo / "feature.py").exists()                    # actually merged
    assert engine.store.get("55").worker_status == "delivered"
    assert engine._pending_delivery == {}                    # no approval needed
    assert not any("🚦" in p.text for p in t.posts)


def test_balanced_mode_still_blocks_failed_checks(tmp_path):
    """Balanced is a soft AUTHORIZATION gate, not a correctness one — failed checks
    still block delivery."""
    engine, t = _engine(tmp_path)
    _worker_with_commit(engine, tmp_path, "gf2", "kite")
    asyncio.run(engine._run_checks("kite", "python -c \"exit(1)\""))   # red
    res = asyncio.run(engine._deliver_worker("kite", "merge"))
    assert res.get("is_error") is True and "FAILED" in res["content"][0]["text"]
    assert engine.store.get(engine._find_worker("kite")[0]).worker_status != "delivered"


def test_turn_actions_drain_into_footer_once(tmp_path):
    engine, _ = _engine(tmp_path)
    assert engine._drain_turn_actions() is None            # nothing ran → no footer
    engine._action("spawn_worker → Nova · myapp")
    engine._action("message_worker(nova)")
    footer = engine._drain_turn_actions()
    assert footer == "⚙ spawn_worker → Nova · myapp · message_worker(nova)"
    assert engine._drain_turn_actions() is None            # drained — no stale leak


def test_pending_approval_survives_restart(tmp_path):
    """A 🚦 prompt issued before a restart must still be approvable after it."""
    engine, t = _engine(tmp_path)
    engine._pending_delivery["nova"] = "merge"
    engine.store.set_pending_delivery(engine._pending_delivery)
    # simulate restart: fresh engine over the same store
    engine2 = Engine(_settings(tmp_path), CoreStore(tmp_path / "state"))
    assert engine2._pending_delivery == {"nova": "merge"}


def test_interrupt_is_honest_about_idle_sessions(tmp_path):
    """'⏹ interrupting…' must only be claimed when something was actually running."""
    engine, _ = _engine(tmp_path)
    fake = FakeSession()                       # status: idle
    engine.sessions["general"] = fake
    assert asyncio.run(engine.interrupt("dm:42")) is False   # idle → nothing to stop
    fake.status = "busy"
    assert asyncio.run(engine.interrupt("dm:42")) is True    # busy → interrupted


def test_spawn_repo_cannot_escape_projects_root(tmp_path):
    """Security: an injected orchestrator must not root a worker outside the projects
    root (e.g. over mounted /root/.claude creds). Absolute out-of-root → refused."""
    engine, _ = _engine(tmp_path)
    _repo(tmp_path, "legit")                                   # projects/legit
    assert engine._resolve_repo("legit") is not None          # in-root name: ok
    outside = tmp_path / "secrets"                             # sibling of projects/
    outside.mkdir()
    assert engine._resolve_repo(str(outside)) is None         # abs out-of-root: refused
    assert engine._resolve_repo("../secrets") is None         # traversal: refused


def test_status_menu_quote_is_not_misread_as_done(tmp_path):
    """A worker QUOTING the STATUS menu mid-reply, then ending on a real status,
    must be read by the last line only — not a substring scan."""
    engine, _ = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", cwd=str(tmp_path), worker_id="nova",
        repo=str(tmp_path), task="x", worker_status="working"))

    class MenuThenBlocked:
        result = ("I'll end with one of STATUS: done | working | blocked as required.\n"
                  "Waiting on the API key.\n"
                  "STATUS: blocked: need the API key")
        is_error = False

    sess = FakeSession(); sess.thread_id = "55"
    asyncio.run(engine._on_worker_turn_done(sess, MenuThenBlocked()))
    assert engine.store.get("55").worker_status == "blocked"   # the LAST line wins


def test_working_status_unsticks_a_done_worker(tmp_path):
    engine, _ = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", cwd=str(tmp_path), worker_id="nova",
        repo=str(tmp_path), task="x", worker_status="done"))

    class BackToWork:
        result = "picking the follow-up back up.\nSTATUS: working"
        is_error = False

    sess = FakeSession(); sess.thread_id = "55"
    asyncio.run(engine._on_worker_turn_done(sess, BackToWork()))
    assert engine.store.get("55").worker_status == "working"


def test_interjection_unsticks_done_worker(tmp_path):
    """A boss follow-up typed straight into a done worker's thread un-sticks it so
    the snapshot/dashboard don't lie while it works again."""
    engine, _ = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(
        role="worker", name="Nova", cwd=str(tmp_path), worker_id="nova",
        repo=str(tmp_path), task="x", worker_status="done"))
    engine.sessions["55"] = FakeSession()
    asyncio.run(engine.on_inbound(InboundMessage(
        thread_id="55", text="also handle negatives", sender_name="Jon")))
    assert engine.store.get("55").worker_status == "working"


def test_delivered_worker_drops_out_of_snapshot(tmp_path):
    engine, _ = _engine(tmp_path)
    engine.store.put("1", ThreadRecord(role="worker", name="A", worker_id="a",
        repo="/r/x", worker_status="delivered"))
    engine.store.put("2", ThreadRecord(role="worker", name="B", worker_id="b",
        repo="/r/x", worker_status="working"))
    snap = engine._fleet_snapshot()
    assert "b=" in snap and "a=" not in snap


def test_status_parse_tolerates_markdown_and_postscript(tmp_path):
    """The last-STATUS-line parse must survive real LLM formatting — bold, bullets,
    quotes, and a trailing courtesy line — not just a bare final line."""
    from beaboss.core.engine import _parse_worker_status as ps
    assert ps("done the thing\n**STATUS: done**") == "done"
    assert ps("- STATUS: done") == "done"
    assert ps("all set.\nSTATUS: done\nThanks! 🎉") == "done"
    assert ps("> STATUS: blocked: need the key") == "blocked"
    assert ps("STATUS: needs-decision: A or B?") == "blocked"
    assert ps("picking it back up\nSTATUS: working") == "working"
    # a quote of the menu is NOT a status; a real one later wins
    assert ps("I'll end with STATUS: done | working | blocked.\nSTATUS: working") == "working"
    assert ps("just a menu quote: STATUS: done | working | blocked") is None
    assert ps("no status line here") is None


def test_none_is_not_eaten_as_a_quiet_sentinel(tmp_path):
    """Regression: a legit one-word 'None.' answer must post, not be suppressed."""
    from beaboss.core.session import _is_quiet_reply
    assert _is_quiet_reply("NOTHING") is True
    assert _is_quiet_reply("(nothing)") is True
    assert _is_quiet_reply("None.") is False      # ordinary answer — must post
    assert _is_quiet_reply("none") is False
    assert _is_quiet_reply("quiet") is False
    assert _is_quiet_reply("Done — see PR") is False


def test_factory_reset_clears_scrollback_and_keeps_the_office(tmp_path):
    """Reported live: after `/reset confirm` the old messages were still on screen.

    Reset must be a true blank slate on the SURFACE too — the transport's replay
    scrollback is dropped (so nothing reappears on reload) and worker threads vanish —
    while the orchestrator's office survives so you land on a fresh, empty conversation
    rather than a threadless void.
    """
    from beaboss.core.ports import Speaker
    from beaboss.transports.websocket import WebSocketTransport

    store = CoreStore(tmp_path / "state")
    store.put("general", ThreadRecord(role="orchestrator", name="orchestrator"))
    store.put("7", ThreadRecord(role="worker", name="Nova", worker_id="nova",
                                repo=str(tmp_path / "r"), worker_status="working"))
    transport = WebSocketTransport(store)
    engine = Engine(_settings(tmp_path), store)
    engine.attach_transport(transport)

    you = Speaker(role="you", name="You", emoji="")
    for i in range(3):
        asyncio.run(transport.post(Outbound(thread_id="general", speaker=you,
                                            text=f"old msg {i}")))
    assert len(transport.history) == 3 and "7" in transport.threads

    asyncio.run(engine.factory_reset())

    assert transport.history == []                 # nothing replays on reload
    assert "7" not in transport.threads            # the worker thread is gone
    assert "general" in transport.threads          # …but the office is still there
    assert transport.dashboard == ""


def test_reset_confirmation_owns_its_surface_caveat(tmp_path):
    """The confirmation must not over-claim a 'blank slate' the surface couldn't
    deliver: a transport that can't clear old messages contributes a caveat, and one
    that fully wipes (web/CLI) doesn't."""
    engine, plain = _engine(tmp_path)          # FakeTransport: no reset_caveat
    msg = asyncio.run(engine.factory_reset())
    assert "Factory reset complete" in msg and "⚠️" not in msg

    plain.reset_caveat = "\n\n⚠️ older messages may remain."
    msg2 = asyncio.run(engine.factory_reset())
    assert msg2.endswith("older messages may remain.")


def test_worker_turn_error_wakes_the_orchestrator(tmp_path, monkeypatch):
    """Self-heal regression (the live JSON-buffer bug): a worker turn that CRASHES has
    no ResultMessage, so on_turn_done never fires — the orchestrator must still be woken
    with a note so it follows up, instead of the worker dying quietly."""
    engine, t = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(role="worker", name="Nova", worker_id="nova",
                                        task="do the art", worker_status="working"))
    woke: list[bool] = []

    async def fake_wake():
        woke.append(True)

    async def anoop(*a, **k):
        pass

    monkeypatch.setattr(engine, "_wake_orchestrator", fake_wake)
    monkeypatch.setattr(engine, "_refresh_dashboard", anoop)

    class CrashedSession:            # crashed but self-reconnected
        thread_id = "55"
        status = "idle"

    err = RuntimeError("Failed to decode JSON: JSON message exceeded maximum "
                       "buffer size of 1048576 bytes")
    asyncio.run(engine._on_worker_turn_error(CrashedSession(), err))

    assert woke == [True]                                   # orchestrator was woken
    notes = " ".join(engine._inbox).lower()
    assert "nova" in notes and "buffer size" in notes       # …and told what broke
    assert "reconnected" in notes                           # recovered → retry/re-brief hint


def test_spawn_worker_records_dispatched_tier_model(tmp_path, monkeypatch):
    """Model dispatch: a tier resolves to a concrete model id, persisted on the worker
    record so it's deterministic and survives a restart."""
    engine, t = _engine(tmp_path)
    engine.settings.model_tiers = {"fast": "haiku", "balanced": "", "deep": "opus"}
    (tmp_path / "projects" / "myrepo").mkdir(parents=True)

    async def fake_is_git(path): return True
    async def fake_branch(path): return "main"
    async def fake_create(*a, **k): return tmp_path / "wt"

    class FS:                       # a fake session so no real backend starts
        async def submit(self, *a, **k): pass
    async def fake_ensure(thread_id, rec): return FS()

    monkeypatch.setattr(worktrees, "is_git_repo", fake_is_git)
    monkeypatch.setattr(worktrees, "current_branch", fake_branch)
    monkeypatch.setattr(worktrees, "create_worktree", fake_create)
    monkeypatch.setattr(engine, "_ensure_session", fake_ensure)

    res = asyncio.run(engine._spawn_worker("myrepo", "rename a var", tier="deep"))
    assert res.get("is_error") is not True
    recs = list(engine.store.workers().values())
    assert len(recs) == 1 and recs[0].model == "opus"     # deep -> opus, persisted


def test_spawn_worker_rejects_bogus_tier(tmp_path):
    """The enum is the allowlist, but _spawn_worker validates defensively too — a bad
    tier errors cleanly and creates nothing."""
    engine, t = _engine(tmp_path)
    res = asyncio.run(engine._spawn_worker("myrepo", "task", tier="ultra"))
    assert res.get("is_error") is True and "tier" in res["content"][0]["text"]
    assert engine.store.workers() == {}


def test_make_worker_session_uses_dispatched_model(tmp_path):
    """The persisted model reaches the CoreSession as its override — restart-proof,
    since _make_worker_session reads rec.model, not the (gone) tier."""
    engine, t = _engine(tmp_path)
    rec = ThreadRecord(role="worker", name="Nova", worker_id="nova",
                       cwd=str(tmp_path), repo=str(tmp_path), model="haiku")
    sess = engine._make_worker_session("55", rec)
    assert sess._model_override == "haiku"


def test_worker_screenshots_are_captured_for_orchestrator_vision(tmp_path):
    """The 'eyes' fix: a worker's posted screenshot is remembered so the orchestrator's
    next supervision turn can SEE it (vision), not just read a text summary."""
    from beaboss.core.ports import Outbound, Speaker
    engine, t = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(role="worker", name="Nova", worker_id="nova"))
    engine.store.put("general", ThreadRecord(role="orchestrator", name="orch"))
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)

    worker = Speaker(role="worker", name="Nova", emoji="⚙️")
    asyncio.run(engine._post(Outbound(thread_id="55", speaker=worker,
                media_path=png, media_kind="photo", caption="round 1")))
    assert engine._pending_vision == [str(png)]            # a worker screenshot is remembered

    items = engine._vision_items(engine._pending_vision)
    assert len(items) == 1 and items[0].kind == "image"
    assert items[0].data.startswith(b"\x89PNG")            # real bytes, ready as vision

    # the orchestrator's own media is NOT fed back to it, and a missing file is skipped
    engine._pending_vision.clear()
    orch = Speaker(role="orchestrator", name="O", emoji="🧭")
    asyncio.run(engine._post(Outbound(thread_id="general", speaker=orch,
                media_path=png, media_kind="photo")))
    assert engine._pending_vision == []
    assert engine._vision_items([str(tmp_path / "gone.png")]) == []


def test_worker_screenshot_is_forwarded_to_the_boss(tmp_path):
    """Reliability fix: a worker's screenshot lands in the boss's OWN chat automatically
    — not only if the orchestrator remembers to send it."""
    from beaboss.core.ports import Outbound, Speaker
    engine, t = _engine(tmp_path)
    engine.store.put("55", ThreadRecord(role="worker", name="Nova", worker_id="nova"))
    engine._last_boss_thread = "dm:123"        # where the boss talks to the orchestrator
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
    worker = Speaker(role="worker", name="Nova", emoji="⚙️")

    asyncio.run(engine._post(Outbound(thread_id="55", speaker=worker,
                media_path=png, media_kind="photo", caption="round 1")))

    fwd = [o for o in t.posts if o.thread_id == "dm:123" and o.media_path == png]
    assert len(fwd) == 1 and "Nova" in (fwd[0].caption or "")   # forwarded to the boss
    assert any(o.thread_id == "55" for o in t.posts)            # original still posted too
