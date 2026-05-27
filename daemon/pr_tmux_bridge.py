#!/usr/bin/env python3
"""Localhost daemon that bridges GitHub PRs to tmux worktree sessions.

Endpoints (GET, JSON responses):
    /health                                  -> {"ok": true} (unauthenticated)
    /userscript.js                           -> the userscript, token injected (unauthenticated)
    /status/<owner>/<repo>/<number>          -> {found, branch} read-only, no side effects
    /open-pr/<owner>/<repo>/<number>         -> resolve PR branch, attach to tmux if found
    /spawn/<owner>/<repo>/<branch>           -> create worktree + tmux session, then attach

All endpoints except /health and /userscript.js require the X-PR-Tmux-Token header.

Settings come from ~/.config/pr-tmux-bridge/config (KEY=value lines), each overridable by an
env var PR_TMUX_BRIDGE_<KEY>. The key one is CREATE_COMMAND: the command that provisions a
worktree + session, run with cwd set to the repo root, with {branch}/{repo_root} substituted
as whole argv tokens (no shell). A reference implementation ships in scripts/create-worktree.sh.

Stdlib only. Run with: python3 pr_tmux_bridge.py
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

PORT = 47811
CONFIG_DIR = os.path.expanduser("~/.config/pr-tmux-bridge")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token")
USERSCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "userscript", "pr-tmux-bridge.user.js")
ALLOWED_HOSTS = frozenset({f"127.0.0.1:{PORT}", f"localhost:{PORT}"})
TOKEN_HEADER = "X-PR-Tmux-Token"
# Settings resolved per request via setting(); these are the built-in defaults.
SETTING_DEFAULTS = {
    "CREATE_COMMAND": "",  # no built-in default; must be configured to spawn
    "TERMINAL_APP": "Ghostty",
    "WORKSPACE": "~/workspace",  # os.pathsep-separated search roots
    "WORKTREE_BASE": "~/wt",  # consumed by the create command, not the daemon
    "REPOS": "{}",  # JSON map {"owner/repo": "/path/to/clone"}
}
LOG = logging.getLogger("pr-tmux-bridge")


def _read_config_file() -> dict[str, str]:
    """Parse the config file (KEY=value lines, # comments). Empty dict if absent."""
    values: dict[str, str] = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def setting(key: str) -> str:
    """Resolve a setting: env PR_TMUX_BRIDGE_<key> > config file <key> > built-in default.

    Read live so edits to the config file take effect without restarting the daemon.
    """
    env = os.environ.get(f"PR_TMUX_BRIDGE_{key}")
    if env is not None and env.strip():
        return env.strip()
    file_val = _read_config_file().get(key)
    if file_val is not None and file_val.strip():
        return file_val.strip()
    return SETTING_DEFAULTS.get(key, "")


def load_or_create_token() -> str:
    """Read the shared secret from TOKEN_PATH, generating it (0600) on first run."""
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            existing = handle.read().strip()
        if existing:
            return existing
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    return token


TOKEN = load_or_create_token()


def load_create_command() -> str | None:
    """The configured provisioning command, or None if unset."""
    return setting("CREATE_COMMAND") or None


def search_roots() -> list[str]:
    """Expanded list of roots to look for local clones under."""
    return [os.path.expanduser(part) for part in setting("WORKSPACE").split(os.pathsep) if part]


def repo_overrides() -> dict[str, str]:
    """Parsed REPOS map, or empty dict on bad JSON."""
    try:
        return json.loads(setting("REPOS"))
    except json.JSONDecodeError:
        return {}


def create_command_env() -> dict[str, str]:
    """Subprocess env for the create command: config settings exported as PR_TMUX_BRIDGE_*.

    Lets the create command (e.g. scripts/create-worktree.sh) read settings like
    WORKTREE_BASE from the config file even when the daemon runs under launchd.
    """
    env = dict(os.environ)
    for key in SETTING_DEFAULTS:
        env[f"PR_TMUX_BRIDGE_{key}"] = setting(key)
    return env


def _run(
    cmd: list[str], check: bool = True, cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=cwd, env=env)


def resolve_pr_branch(owner: str, repo: str, number: int) -> str:
    """Return the head branch name of a PR via gh."""
    result = _run(["gh", "pr", "view", str(number), "-R", f"{owner}/{repo}", "--json", "headRefName"])
    return json.loads(result.stdout)["headRefName"]


def _list_tmux_sessions() -> list[tuple[str, str]]:
    """Return [(session_name, session_path), ...] from tmux, or [] if no server."""
    result = _run(["tmux", "list-sessions", "-F", "#{session_name}\t#{session_path}"], check=False)
    if result.returncode != 0:
        return []
    pairs = []
    for line in result.stdout.splitlines():
        name, _, path = line.partition("\t")
        if name:
            pairs.append((name, path))
    return pairs


def find_session_for_branch(branch: str) -> str | None:
    """Return the tmux session whose cwd is a git checkout on `branch`, or None.

    Scanning real tmux sessions (rather than asking a worktree manager) means the
    returned name is exactly what tmux knows it as, including any emoji prefix, so it
    can be handed straight to `tmux switch-client -t`.
    """
    for name, path in _list_tmux_sessions():
        if not path:
            continue
        head = _run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], check=False)
        if head.returncode == 0 and head.stdout.strip() == branch:
            return name
    return None


def has_attached_client() -> bool:
    result = _run(["tmux", "list-clients", "-F", "#{client_name}"], check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def focus_terminal() -> None:
    subprocess.run(
        ["osascript", "-e", f'tell application "{setting("TERMINAL_APP")}" to activate'],
        check=False,
    )


def attach_session(session: str) -> None:
    """Switch the attached tmux client to `session`, or spawn a new terminal window if none."""
    if has_attached_client():
        _run(["tmux", "switch-client", "-t", session], check=False)
        focus_terminal()
        return
    # No attached client: open a new terminal window running `tmux attach`.
    subprocess.run(
        ["open", "-na", setting("TERMINAL_APP"), "--args", "-e", f"tmux attach -t {session}"],
        check=False,
    )


def read_pr_status(owner: str, repo: str, number: int) -> tuple[str, str | None]:
    """Read-only: return (branch, session_or_none) for a PR. No side effects."""
    branch = resolve_pr_branch(owner, repo, number)
    return branch, find_session_for_branch(branch)


def _is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def _origin_slug(path: str) -> tuple[str, str] | None:
    """Parse (owner, repo) from a clone's origin remote URL, or None."""
    result = _run(["git", "-C", path, "remote", "get-url", "origin"], check=False)
    if result.returncode != 0:
        return None
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", result.stdout.strip())
    return (match.group(1), match.group(2)) if match else None


def find_repo_root(owner: str, repo: str) -> str | None:
    """Resolve a local clone path for owner/repo.

    Order: explicit override map, then <root>/<repo> by convention, then a scan of each
    search root matching the clone's `origin` URL (handles dir name != repo name).
    """
    overrides = repo_overrides()
    key = f"{owner}/{repo}"
    if key in overrides:
        path = os.path.expanduser(overrides[key])
        return path if _is_git_repo(path) else None

    roots = search_roots()
    for root in roots:
        candidate = os.path.join(root, repo)
        if _is_git_repo(candidate):
            return candidate

    for root in roots:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and _is_git_repo(entry.path) and _origin_slug(entry.path) == (owner, repo):
                return entry.path
    return None


def ensure_local_branch_tracks_remote(repo_root: str, branch: str) -> None:
    """Make sure `repo_root` has a local branch named `branch` tracking origin/<branch>.

    Runs before the create command so that a `git worktree add <branch>` checks out the
    PR's actual commits. Without an existing local branch, a provisioner may instead create
    a fresh branch off the default base (e.g. origin/main), losing the PR's work.

    - Fetches origin/<branch> to ensure the remote tracking ref is current.
    - Creates a local tracking branch if missing.
    - Fast-forwards an existing local branch only if it's strictly an ancestor of
      origin/<branch> (never overwrites diverged local work).
    """
    subprocess.run(
        ["git", "-C", repo_root, "fetch", "origin", branch],
        capture_output=True,
        text=True,
        check=True,
    )
    local_exists = (
        subprocess.run(
            ["git", "-C", repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode
        == 0
    )
    if not local_exists:
        subprocess.run(
            ["git", "-C", repo_root, "branch", "--track", branch, f"origin/{branch}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return
    local_is_ancestor = (
        subprocess.run(
            ["git", "-C", repo_root, "merge-base", "--is-ancestor", branch, f"origin/{branch}"],
            check=False,
        ).returncode
        == 0
    )
    if local_is_ancestor:
        subprocess.run(
            ["git", "-C", repo_root, "branch", "-f", branch, f"origin/{branch}"],
            capture_output=True,
            text=True,
            check=True,
        )


def build_create_argv(command: str, branch: str, repo_root: str) -> list[str]:
    """Split `command` and substitute {branch}/{repo_root} as whole argv tokens (no shell)."""
    return [token.replace("{branch}", branch).replace("{repo_root}", repo_root) for token in shlex.split(command)]


def create_session(command: str, repo_root: str, branch: str, timeout_s: float = 8.0) -> str | None:
    """Provision a worktree + tmux session for `branch`, returning the tmux session name.

    Runs `command` with cwd=repo_root, then polls until the session shows up (a provisioner
    may register the tmux session asynchronously after creating the worktree).
    """
    ensure_local_branch_tracks_remote(repo_root, branch)
    _run(build_create_argv(command, branch, repo_root), cwd=repo_root, env=create_command_env())
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        session = find_session_for_branch(branch)
        if session:
            return session
        time.sleep(0.3)
    return None


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _valid_host(self) -> bool:
        """Reject requests whose Host isn't loopback (defends against DNS rebinding)."""
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _authorized(self) -> bool:
        supplied = self.headers.get(TOKEN_HEADER, "")
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Only the userscript (via GM.xmlHttpRequest, which bypasses CORS) talks to us.
        # A browser preflight here means a plain cross-site fetch is probing us — deny it
        # by responding without any Access-Control-Allow-* grant.
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        # Unauthenticated endpoints.
        if path == "/health":
            self._json(200, {"ok": True})
            return
        if path == "/userscript.js":
            self._serve_userscript()
            return

        # Everything below is authenticated and loopback-only.
        if not self._valid_host():
            self._json(403, {"error": "forbidden host"})
            return
        if not self._authorized():
            self._json(401, {"error": "missing or invalid token"})
            return

        match = re.match(r"^/status/([^/]+)/([^/]+)/(\d+)$", path)
        if match:
            self._status(match.group(1), match.group(2), int(match.group(3)))
            return

        match = re.match(r"^/open-pr/([^/]+)/([^/]+)/(\d+)$", path)
        if match:
            self._open_pr(match.group(1), match.group(2), int(match.group(3)))
            return

        match = re.match(r"^/spawn/([^/]+)/([^/]+)/(.+)$", path)
        if match:
            self._spawn(match.group(1), match.group(2), unquote(match.group(3)))
            return

        self._json(404, {"error": "not found"})

    def _serve_userscript(self) -> None:
        try:
            with open(USERSCRIPT_PATH, encoding="utf-8") as handle:
                source = handle.read()
        except OSError as exc:
            self._json(500, {"error": "userscript not found", "detail": str(exc)})
            return
        body = source.replace("__TOKEN__", TOKEN).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _status(self, owner: str, repo: str, number: int) -> None:
        try:
            branch, session = read_pr_status(owner, repo, number)
        except subprocess.CalledProcessError as exc:
            self._json(502, {"error": "gh pr view failed", "stderr": exc.stderr})
            return
        self._json(200, {"found": bool(session), "branch": branch, "session": session})

    def _open_pr(self, owner: str, repo: str, number: int) -> None:
        try:
            branch, session = read_pr_status(owner, repo, number)
        except subprocess.CalledProcessError as exc:
            self._json(502, {"error": "gh pr view failed", "stderr": exc.stderr})
            return
        if session:
            attach_session(session)
            self._json(200, {"found": True, "branch": branch, "session": session})
        else:
            self._json(200, {"found": False, "branch": branch})

    def _spawn(self, owner: str, repo: str, branch: str) -> None:
        command = load_create_command()
        if not command:
            self._json(
                400,
                {
                    "error": "no create command configured",
                    "hint": (
                        "set PR_TMUX_BRIDGE_CREATE_COMMAND or write the command to "
                        "~/.config/pr-tmux-bridge/create-command"
                    ),
                },
            )
            return
        repo_root = find_repo_root(owner, repo)
        if not repo_root:
            self._json(
                404,
                {
                    "error": "no local clone found",
                    "repo": f"{owner}/{repo}",
                    "searched": search_roots(),
                    "hint": 'set PR_TMUX_BRIDGE_REPOS env var, e.g. {"owner/repo": "/path/to/clone"}',
                },
            )
            return
        try:
            session = create_session(command, repo_root, branch)
        except subprocess.CalledProcessError as exc:
            LOG.error("create command failed: %s", (exc.stderr or "").strip())
            self._json(502, {"error": "create command failed", "stderr": exc.stderr})
            return
        if not session:
            self._json(500, {"error": "session not found after create", "branch": branch})
            return
        attach_session(session)
        self._json(200, {"spawned": True, "branch": branch, "session": session, "repo_root": repo_root})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        LOG.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    LOG.info("pr-tmux-bridge listening on http://127.0.0.1:%d", PORT)
    LOG.info("create command: %s", load_create_command() or "(none configured)")
    LOG.info("install/update userscript from http://127.0.0.1:%d/userscript.js", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
