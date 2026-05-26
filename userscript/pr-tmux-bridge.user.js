// ==UserScript==
// @name         PR → tmux bridge
// @namespace    https://github.com/matthieudesprez/pr-tmux-bridge
// @version      0.1.0
// @description  Jump from a GitHub PR to its tmux session, or spawn a sarj worktree.
// @match        https://github.com/*/*/pull/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(() => {
	const DAEMON = "http://127.0.0.1:47811";
	const BUTTON_ID = "pr-tmux-bridge-btn";

	function parsePr() {
		const match = location.pathname.match(/^\/([^/]+)\/([^/]+)\/pull\/(\d+)/);
		if (!match) return null;
		return { owner: match[1], repo: match[2], number: Number(match[3]) };
	}

	async function callDaemon(path) {
		const response = await fetch(`${DAEMON}${path}`);
		if (!response.ok) {
			throw new Error(`daemon returned ${response.status}`);
		}
		return response.json();
	}

	async function onClick(event) {
		event.preventDefault();
		const pr = parsePr();
		if (!pr) return;
		const button = event.currentTarget;
		const originalText = button.textContent;
		button.disabled = true;
		button.textContent = "…";
		try {
			const data = await callDaemon(
				`/open-pr/${pr.owner}/${pr.repo}/${pr.number}`,
			);
			if (data.found) return;
			const branch = data.branch;
			if (
				!confirm(
					`No tmux session for branch "${branch}". Spawn a sarj worktree?`,
				)
			)
				return;
			button.textContent = "spawning…";
			await callDaemon(`/spawn-sarj/${encodeURIComponent(branch)}`);
		} catch (err) {
			alert(
				`pr-tmux-bridge daemon unreachable.\n\n${err.message}\n\nIs the daemon running on ${DAEMON}?`,
			);
		} finally {
			button.disabled = false;
			button.textContent = originalText;
		}
	}

	function buildButton() {
		const button = document.createElement("button");
		button.id = BUTTON_ID;
		button.type = "button";
		button.textContent = "→ tmux";
		button.title = "Open this PR in tmux (or spawn a sarj worktree)";
		button.className = "btn btn-sm";
		button.style.marginLeft = "8px";
		button.addEventListener("click", onClick);
		return button;
	}

	function inject() {
		if (document.getElementById(BUTTON_ID)) return;
		const target =
			document.querySelector(".gh-header-actions") ||
			document.querySelector('[data-component="PH_Actions"]') ||
			document.querySelector(".gh-header-title");
		if (!target) return;
		target.appendChild(buildButton());
	}

	const observer = new MutationObserver(() => inject());
	observer.observe(document.body, { childList: true, subtree: true });
	inject();
})();
