#!/usr/bin/env python3
"""Localhost daemon that bridges GitHub PRs to tmux/sarj sessions.

Endpoints (GET, JSON responses):
    /open-pr/<owner>/<repo>/<number>   -> resolve PR branch, attach to tmux if found
    /spawn-sarj/<branch>               -> sarj create <branch> --no-attach, then attach
    /health                            -> {"ok": true}

Stdlib only. Run with: python3 pr_tmux_bridge.py
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

PORT = 47811
LOG = logging.getLogger("pr-tmux-bridge")


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def resolve_pr_branch(owner: str, repo: str, number: int) -> str:
    """Return the head branch name of a PR via gh."""
    result = _run(["gh", "pr", "view", str(number), "-R", f"{owner}/{repo}", "--json", "headRefName"])
    return json.loads(result.stdout)["headRefName"]


def find_sarj_session(branch: str) -> tuple[str, str] | None:
    """Look up a sarj worktree by branch. Returns (session_name, worktree_path) or None."""
    result = _run(["sarj", "list", "-o", "json"], check=False)
    if result.returncode != 0:
        return None
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    for entry in entries:
        if entry.get("branch") == branch:
            session = (entry.get("tmux") or {}).get("session")
            path = entry.get("path")
            if session and path:
                return session, path
    return None


def find_tmux_session_by_path(branch: str) -> str | None:
    """Fallback: scan all tmux sessions, return one whose cwd is on `branch`."""
    result = _run(["tmux", "list-sessions", "-F", "#{session_name}\t#{session_path}"], check=False)
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        name, _, path = line.partition("\t")
        if not path:
            continue
        head = _run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], check=False)
        if head.returncode == 0 and head.stdout.strip() == branch:
            return name
    return None


def find_session_for_branch(branch: str) -> str | None:
    sarj = find_sarj_session(branch)
    if sarj:
        return sarj[0]
    return find_tmux_session_by_path(branch)


def has_attached_client() -> bool:
    result = _run(["tmux", "list-clients", "-F", "#{client_name}"], check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def focus_terminal() -> None:
    subprocess.run(["osascript", "-e", 'tell application "iTerm" to activate'], check=False)


def attach_session(session: str) -> None:
    """Switch the attached tmux client to `session`, or spawn a new iTerm window if none."""
    if has_attached_client():
        _run(["tmux", "switch-client", "-t", session], check=False)
        focus_terminal()
        return
    # No attached client: open a new iTerm window running `tmux attach`.
    applescript = (
        'tell application "iTerm"\n'
        "    activate\n"
        "    set newWindow to (create window with default profile)\n"
        '    tell current session of newWindow to write text "tmux attach -t ' + session.replace('"', '\\"') + '"\n'
        "end tell\n"
    )
    subprocess.run(["osascript", "-e", applescript], check=False)


def spawn_sarj_worktree(branch: str) -> str | None:
    """Create a sarj worktree on `branch` and return its tmux session name."""
    _run(["sarj", "create", branch, "--no-attach"])
    sarj = find_sarj_session(branch)
    return sarj[0] if sarj else None


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/health":
            self._json(200, {"ok": True})
            return

        match = re.match(r"^/open-pr/([^/]+)/([^/]+)/(\d+)$", path)
        if match:
            self._open_pr(match.group(1), match.group(2), int(match.group(3)))
            return

        match = re.match(r"^/spawn-sarj/(.+)$", path)
        if match:
            self._spawn_sarj(unquote(match.group(1)))
            return

        self._json(404, {"error": "not found"})

    def _open_pr(self, owner: str, repo: str, number: int) -> None:
        try:
            branch = resolve_pr_branch(owner, repo, number)
        except subprocess.CalledProcessError as exc:
            self._json(502, {"error": "gh pr view failed", "stderr": exc.stderr})
            return
        session = find_session_for_branch(branch)
        if session:
            attach_session(session)
            self._json(200, {"found": True, "branch": branch, "session": session})
        else:
            self._json(200, {"found": False, "branch": branch})

    def _spawn_sarj(self, branch: str) -> None:
        try:
            session = spawn_sarj_worktree(branch)
        except subprocess.CalledProcessError as exc:
            self._json(502, {"error": "sarj create failed", "stderr": exc.stderr})
            return
        if not session:
            self._json(500, {"error": "session not found after sarj create", "branch": branch})
            return
        attach_session(session)
        self._json(200, {"spawned": True, "branch": branch, "session": session})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        LOG.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    LOG.info("pr-tmux-bridge listening on http://127.0.0.1:%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
