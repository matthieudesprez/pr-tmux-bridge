#!/usr/bin/env bash
# Reference provisioning command: create a git worktree for a branch and a detached
# tmux session in it. The daemon runs this with cwd set to the repo root and the
# branch as $1, then attaches to the resulting session.
#
# Wire it up as your create command:
#   echo '/abs/path/to/scripts/create-worktree.sh {branch}' > ~/.config/pr-tmux-bridge/create-command
#
# Worktree location defaults to ~/wt/<repo>/<branch>; override with PR_TMUX_BRIDGE_WORKTREE_BASE.
set -euo pipefail

branch="${1:?usage: create-worktree.sh <branch>}"
repo_root="$(git rev-parse --show-toplevel)"
repo_name="$(basename "$repo_root")"
worktree_base="${PR_TMUX_BRIDGE_WORKTREE_BASE:-$HOME/wt}"
session="${branch//\//-}"
worktree_path="$worktree_base/$repo_name/$session"

if [ ! -d "$worktree_path" ]; then
	mkdir -p "$(dirname "$worktree_path")"
	# The daemon has already created/fast-forwarded the local branch.
	git worktree add "$worktree_path" "$branch"
fi

if ! tmux has-session -t "=$session" 2>/dev/null; then
	tmux new-session -d -s "$session" -c "$worktree_path"
fi
