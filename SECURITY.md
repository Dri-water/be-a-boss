# Security

be-a-boss runs **coding-agent sessions (Claude Code or Codex) with
`bypassPermissions`** — i.e. it executes code and shell commands on the host that
runs it — driven from whichever surface you choose (Telegram, web, or VS Code).
Read this before deploying.

## Threat model

**What protects you:**

1. **User allowlist.** `TELEGRAM_ALLOWED_USER_IDS` is enforced on every update; the
   bot silently ignores anyone not listed. With an empty allowlist it starts in
   **setup mode** — every command except `/whoami` is refused, so it is never
   open-to-all — letting you learn your id (`/whoami`) and lock it down before
   granting access. This is the primary access control — keep it tight. Anyone can
   *find* a public bot, but only allowlisted user IDs can drive it. (The web/VS Code
   surface has no allowlist; its boundary is a localhost-only bind — see README.)
2. **The container boundary.** Running in Docker (see README) is what makes
   `bypassPermissions` reasonable. Sessions can only touch what you mount — your
   projects at `/workspace` — not the rest of the host (SSH keys, credential
   stores, other drives, personal files). **Do not mount the Docker socket**; that
   dissolves the boundary. Do not run the bot directly on the host unless you
   accept that sessions can touch anything your user can.
3. **Secrets stay out of the repo.** `.env` and `state/` are gitignored. The bot
   token, your user ID, and Claude credentials are never committed.

**What you are still exposed to:**

- **Anything under `/workspace` is fair game.** A session can modify or delete any
  project you mount. Rely on git to recover.
- **Prompt injection → exfiltration.** A `bypassPermissions` session with network
  access that reads attacker-controlled content (a malicious repo, a fetched web
  page) could be induced to leak whatever it can read — including its own Claude
  credentials mounted at `/root/.claude`. Mitigations: prefer a dedicated,
  revocable `claude setup-token` over mounting your primary login; don't point
  sessions at untrusted repos; keep the allowlist to people you trust.
- **Telegram is the transport.** Messages and streamed output pass through
  Telegram's servers. Don't paste secrets you wouldn't put in a Telegram chat.

## Audit what you mount

Mounting a broad folder (a whole `Documents/` or home directory) puts **every
loose file at its root** in scope for `bypassPermissions` sessions. Before you rely
on it:

```bash
# list loose files at the mount root — move anything sensitive out of it
docker compose exec be-a-boss sh -c "find /workspace -maxdepth 1 -type f"
```

- Move recovery keys, `*.pem`, exported secrets, financial docs, etc. out of the
  mounted root. Repo folders are fine; stray sensitive files are the risk.
- OS "library" junctions (Windows `My Pictures`/`My Videos`/`My Music`) typically
  appear as symlinks to paths *outside* the mount — they dangle at the container
  boundary and are not reachable. Verify:
  `docker compose exec be-a-boss ls '/workspace/My Pictures'`.
- When in doubt, mount a dedicated code directory rather than your whole home.

## Publishing this code safely

Making the implementation public does **not** weaken a correctly configured
deployment: security rests on your secret bot token and the allowlist, neither of
which is in the source. Before flipping a repo from private to public:

- [ ] `git log -p` / `git grep` for tokens, user IDs, absolute home paths, or any
      `.env` that was ever committed. Scrub history (e.g. `git filter-repo`) if found.
- [ ] Confirm `.gitignore` covers `.env`, `state/`, `*.log`, and `.venv/`.
- [ ] Confirm no real values remain in `.env.example`, `docker-compose.yml`, or docs.
- [ ] Rotate the bot token via @BotFather if it was ever pasted anywhere shareable.

## Reporting a vulnerability

Please report security issues privately to the maintainer (open a GitHub security
advisory or direct message) rather than filing a public issue.
