# pr-tmux-bridge

Jump from a GitHub PR (in the browser) to its tmux session. If no session
exists for the PR's branch, optionally spawn a worktree + tmux session on the
fly via a provisioning command you configure.

## Pieces

- `daemon/pr_tmux_bridge.py` — localhost HTTP daemon (Python 3, stdlib only)
- `userscript/pr-tmux-bridge.user.js` — Tampermonkey/Violentmonkey userscript
  that injects a `→ tmux` button on GitHub PR pages
- `launchd/be.lizy.pr-tmux-bridge.plist` — launchd template (substituted by
  `install.sh`)
- `install.sh` — installs the launchd agent
- `scripts/create-worktree.sh` — reference provisioning command (see below)

## Install (macOS)

```bash
./install.sh
```

Then install the userscript by opening the daemon's self-hosted copy (which
bakes in the auth token):

```bash
open 'http://127.0.0.1:47811/userscript.js'
```

Tampermonkey will prompt to confirm the install. Open any GitHub PR — a
`● tmux` button should appear next to the PR title, with a status dot:
green = a session already exists (click to jump), grey = none (click to spawn).

Do **not** hand-paste `userscript/pr-tmux-bridge.user.js`: its token is a
`__TOKEN__` placeholder and the daemon will reject it with 401. Always install
from the daemon URL. Tampermonkey auto-updates from the same URL (`@updateURL`),
so future versions land with one "Check for userscript updates" click.

## Uninstall

```bash
./install.sh --uninstall
```

## How it works

1. Userscript injects a button on `github.com/*/*/pull/*` pages and queries
   `/status` to color the dot.
2. Click → `GET /open-pr/<owner>/<repo>/<num>`.
3. Daemon resolves the PR's head branch via `gh pr view`.
4. Daemon looks up a matching session by scanning `tmux list-sessions` and
   checking each session's checkout via `git rev-parse`. This returns the exact
   tmux session name (emoji prefix and all), no matter how the session was made.
5. If found: `tmux switch-client -t <session>` + activate the terminal app.
   If no client is attached: open a new terminal window running `tmux attach`.
6. If not found: userscript shows a confirm dialog. On yes, calls `/spawn/...`,
   which fast-forwards the local branch to `origin/<branch>`, runs the configured
   create command, then attaches.

## Provisioning command

Creating a worktree + tmux session is a command **you configure** — there's no
built-in default. The daemon resolves it from, in order:

1. the env var `PR_TMUX_BRIDGE_CREATE_COMMAND`
2. the file `~/.config/pr-tmux-bridge/create-command` (first non-comment line)

It runs with cwd set to the repo root. The tokens `{branch}` and `{repo_root}`
are substituted as whole argv elements after `shlex.split` — there's no shell,
so a branch name can't be interpreted as a command.

`install.sh` seeds the config file with the bundled reference script, which does
`git worktree add` + `tmux new-session`:

```
/abs/path/scripts/create-worktree.sh {branch}
```

Swap that line for your own worktree manager if you use one (anything that
provisions a checkout on `{branch}` and starts a tmux session in it works — the
daemon just polls until a session for the branch appears). The config file is
read live, so edits take effect without restarting the daemon.

## Repo resolution

`/spawn` needs the PR's local clone. It's resolved in order:

1. `PR_TMUX_BRIDGE_REPOS` override map
2. `<root>/<repo>` by convention (for each search root)
3. a scan of each search root matching the clone's `origin` remote URL against
   `owner/repo` — so it works even when the local folder name differs from the
   GitHub repo name

## Security

The daemon binds to `127.0.0.1` only. All endpoints except `/health` and
`/userscript.js` require an `X-PR-Tmux-Token` header matching the secret in
`~/.config/pr-tmux-bridge/token` (generated on first run, mode 0600). The
daemon injects that token into the userscript when serving `/userscript.js`.
Requests with a non-loopback `Host` header are rejected (DNS-rebinding
defense), and CORS preflights are denied (the userscript uses
`GM.xmlHttpRequest`, which bypasses CORS, so a preflight only ever comes from a
cross-site `fetch` probe).

## Configuration (env vars on the launchd plist or shell)

| Var | Default | Purpose |
|---|---|---|
| `PR_TMUX_BRIDGE_TERMINAL_APP` | `Ghostty` | terminal app to focus/spawn |
| `PR_TMUX_BRIDGE_WORKSPACE` | `~/workspace` | search root(s) for clones (`os.pathsep`-separated) |
| `PR_TMUX_BRIDGE_REPOS` | `{}` | JSON map `{"owner/repo": "/path/to/clone"}` for non-convention paths |
| `PR_TMUX_BRIDGE_CREATE_COMMAND` | (config file) | provisioning command; overrides the config file ({branch}, {repo_root} tokens) |
| `PR_TMUX_BRIDGE_WORKTREE_BASE` | `~/wt` | read by `scripts/create-worktree.sh` for worktree location |

## Endpoints

| Method | Path | Auth | Returns |
|---|---|---|---|
| GET | `/health` | no | `{ok: true}` |
| GET | `/userscript.js` | no | userscript with token injected |
| GET | `/status/<owner>/<repo>/<num>` | yes | `{found, branch, session?}` (read-only) |
| GET | `/open-pr/<owner>/<repo>/<num>` | yes | `{found, branch, session?}` (+ attaches) |
| GET | `/spawn/<owner>/<repo>/<branch>` | yes | `{spawned, branch, session, repo_root}` |

## Portability

macOS-only today. The portable bits (userscript, daemon HTTP, gh/tmux/git
shell-outs) are isolated; the OS-specific bits (`focus_terminal`,
`attach_session` when no client, launchd plist) are confined to small
functions and can be swapped for Linux (`wmctrl`/`gnome-terminal` + systemd
user unit) or Windows (`wt.exe` + Task Scheduler) later.
