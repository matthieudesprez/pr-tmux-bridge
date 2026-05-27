// ==UserScript==
// @name         PR → tmux bridge
// @namespace    https://github.com/matthieudesprez/pr-tmux-bridge
// @version      0.3.1
// @description  Jump from a GitHub PR to its tmux session, or spawn a worktree session.
// @match        https://github.com/*/*/pull/*
// @run-at       document-idle
// @grant        GM.xmlHttpRequest
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @updateURL    http://127.0.0.1:47811/userscript.js
// @downloadURL  http://127.0.0.1:47811/userscript.js
// ==/UserScript==

(() => {
	const DAEMON = "http://127.0.0.1:47811";
	// Injected by the daemon when served from /userscript.js. A literal "__TOKEN__"
	// means this copy was installed by hand — reinstall from the daemon URL.
	const TOKEN = "__TOKEN__";
	const BUTTON_ID = "pr-tmux-bridge-btn";
	const DOT_COLORS = {
		found: "#3fb950",
		absent: "#6e7681",
		pending: "#9e6a03",
		error: "#f85149",
	};

	function parsePr() {
		const match = location.pathname.match(/^\/([^/]+)\/([^/]+)\/pull\/(\d+)/);
		if (!match) return null;
		return { owner: match[1], repo: match[2], number: Number(match[3]) };
	}

	const gmRequest =
		typeof GM !== "undefined" && GM.xmlHttpRequest
			? GM.xmlHttpRequest.bind(GM)
			: typeof GM_xmlhttpRequest !== "undefined"
				? GM_xmlhttpRequest
				: null;

	function callDaemon(path) {
		const url = `${DAEMON}${path}`;
		if (!gmRequest) {
			return Promise.reject(
				new Error(
					"GM.xmlHttpRequest not available — set @grant GM.xmlHttpRequest in the userscript header",
				),
			);
		}
		return new Promise((resolve, reject) => {
			gmRequest({
				method: "GET",
				url,
				headers: { "X-PR-Tmux-Token": TOKEN },
				timeout: 15000,
				onload: (response) => {
					let body = null;
					try {
						body = JSON.parse(response.responseText);
					} catch {
						/* non-JSON body */
					}
					if (response.status < 200 || response.status >= 300) {
						const detail =
							(body && (body.error || body.stderr)) || response.responseText;
						const err = new Error(
							`daemon ${response.status}: ${detail || "(no body)"}`,
						);
						err.body = body;
						err.status = response.status;
						reject(err);
						return;
					}
					if (body === null) {
						reject(new Error(`invalid JSON from daemon`));
						return;
					}
					resolve(body);
				},
				onerror: () => reject(new Error(`network error contacting ${url}`)),
				ontimeout: () => reject(new Error(`timeout contacting ${url}`)),
			});
		});
	}

	function setDot(button, state) {
		const dot = button.querySelector(".pr-tmux-dot");
		if (dot) dot.style.color = DOT_COLORS[state] || DOT_COLORS.absent;
	}

	async function refreshStatus(button, pr) {
		setDot(button, "pending");
		try {
			const data = await callDaemon(
				`/status/${pr.owner}/${pr.repo}/${pr.number}`,
			);
			setDot(button, data.found ? "found" : "absent");
			button.title = data.found
				? `tmux session exists for ${data.branch} — click to jump`
				: `no session for ${data.branch} — click to spawn a worktree`;
		} catch (err) {
			setDot(button, "error");
			button.title = `pr-tmux-bridge: ${err.message}`;
		}
	}

	async function onClick(event) {
		event.preventDefault();
		const pr = parsePr();
		if (!pr) return;
		const button = event.currentTarget;
		const label = button.querySelector(".pr-tmux-label");
		const original = label.textContent;
		button.disabled = true;
		label.textContent = "…";
		try {
			const data = await callDaemon(
				`/open-pr/${pr.owner}/${pr.repo}/${pr.number}`,
			);
			if (!data.found) {
				if (
					confirm(
						`No tmux session for branch "${data.branch}". Spawn a worktree?`,
					)
				) {
					label.textContent = "spawning…";
					await callDaemon(
						`/spawn/${pr.owner}/${pr.repo}/${encodeURIComponent(data.branch)}`,
					);
				}
			}
		} catch (err) {
			alert(`pr-tmux-bridge: ${err.message}`);
		} finally {
			button.disabled = false;
			label.textContent = original;
			refreshStatus(button, pr);
		}
	}

	function buildButton() {
		const button = document.createElement("button");
		button.id = BUTTON_ID;
		button.type = "button";
		button.className = "btn btn-sm";
		button.style.marginLeft = "8px";

		const dot = document.createElement("span");
		dot.className = "pr-tmux-dot";
		dot.textContent = "●";
		dot.style.marginRight = "5px";
		dot.style.color = DOT_COLORS.absent;
		button.appendChild(dot);

		const label = document.createElement("span");
		label.className = "pr-tmux-label";
		label.textContent = "tmux";
		button.appendChild(label);

		button.addEventListener("click", onClick);
		return button;
	}

	function inject() {
		const pr = parsePr();
		if (!pr) return;
		const prKey = `${pr.owner}/${pr.repo}/${pr.number}`;

		const existing = document.getElementById(BUTTON_ID);
		if (existing) {
			if (existing.dataset.prKey !== prKey) {
				existing.dataset.prKey = prKey;
				refreshStatus(existing, pr);
			}
			return;
		}

		const target =
			document.querySelector(".gh-header-actions") ||
			document.querySelector('[data-component="PH_Actions"]') ||
			document.querySelector(".gh-header-title");
		if (!target) return;

		const button = buildButton();
		button.dataset.prKey = prKey;
		target.appendChild(button);
		refreshStatus(button, pr);
	}

	const observer = new MutationObserver(() => inject());
	observer.observe(document.body, { childList: true, subtree: true });
	inject();
})();
