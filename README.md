# pr-tmux-bridge

Jump from a GitHub PR (in the browser) to its tmux session. If no session
exists for the PR's branch, optionally spawn a `sarj` worktree on the fly.

## Pieces

- `daemon/pr_tmux_bridge.py` — localhost HTTP daemon (Python 3, stdlib only)
- `userscript/pr-tmux-bridge.user.js` — Tampermonkey/Violentmonkey userscript
  that injects a `→ tmux` button on GitHub PR pages
- `launchd/be.lizy.pr-tmux-bridge.plist` — launchd template (substituted by
  `install.sh`)
- `install.sh` — installs the launchd agent

## Install (macOS)

```bash
./install.sh
```

Then, in Tampermonkey, create a new script and paste the contents of
`userscript/pr-tmux-bridge.user.js`.

Open any GitHub PR — a `→ tmux` button should appear next to the PR title.

## Uninstall

```bash
./install.sh --uninstall
```

## How it works

1. Userscript injects a button on `github.com/*/*/pull/*` pages.
2. Click → `GET http://127.0.0.1:47811/open-pr/<owner>/<repo>/<num>`.
3. Daemon resolves the PR's head branch via `gh pr view`.
4. Daemon looks up a matching session, in order:
   - `sarj list -o json` (matches on `branch`)
   - raw `tmux list-sessions` + `git rev-parse` on each session's `cwd`
5. If found: `tmux switch-client -t <session>` + activate iTerm.
   If no client is attached: open a new iTerm window running `tmux attach`.
6. If not found: userscript shows a confirm dialog. On yes, calls
   `/spawn-sarj/<branch>` → `sarj create <branch> --no-attach` → attach.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | `{ok: true}` |
| GET | `/open-pr/<owner>/<repo>/<num>` | `{found, branch, session?}` |
| GET | `/spawn-sarj/<branch>` | `{spawned, branch, session}` |

## Portability

macOS-only today. The portable bits (userscript, daemon HTTP, gh/tmux/sarj
shell-outs) are isolated; the OS-specific bits (`focus_terminal`,
`attach_session` when no client, launchd plist) are confined to small
functions and can be swapped for Linux (`wmctrl`/`gnome-terminal` + systemd
user unit) or Windows (`wt.exe` + Task Scheduler) later.
