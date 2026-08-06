/*
 * JellyFix secure injector for Jellyfin Web.
 * Install this once in Jellyfin Web and serve the backend at /jellyfix.
 */
(function () {
    "use strict";

    const API_BASE = `${window.location.origin}/jellyfix/api/v1`;
    const ISSUE_TYPES = [
        ["audio", "Audio"],
        ["subtitles", "Subtitles"],
        ["video_quality", "Video quality"],
        ["wrong_language", "Wrong language"],
        ["other", "Other"]
    ];
    const TEXT = {
        report: "Report Issue",
        ticket: "Ticket",
        manager: "Ticket Manager",
        send: "Send",
        close: "Close",
        details: "Details",
        issueType: "Issue type",
        messagePlaceholder: "Describe the problem...",
        authExpired: "Jellyfin login expired. Refresh the page and sign in again.",
        resolved: "This ticket is resolved.",
        noTickets: "No tickets found.",
        loadError: "Unable to load JellyFix.",
        duplicate: "A ticket already exists for this media.",
        inProgress: "Set in progress",
        resolve: "Resolve",
        reopen: "Reopen",
        statusNew: "New",
        statusProgress: "In progress",
        statusResolved: "Resolved"
    };

    let activeAbort = new AbortController();
    let currentItemId = null;
    let meCache = null;
    let observerTimer = null;
    let refreshInFlight = false;

    function addStyles() {
        if (document.getElementById("jellyfix-style")) return;
        const style = document.createElement("style");
        style.id = "jellyfix-style";
        style.textContent = `
            #jellyfix-overlay { display:none; position:fixed; inset:0; z-index:10000; background:rgba(0,0,0,.82); align-items:center; justify-content:center; font-family:Arial,sans-serif; }
            .jellyfix-modal { width:min(760px,92vw); max-height:84vh; background:#181818; color:#fff; border:1px solid #333; border-radius:8px; display:flex; flex-direction:column; overflow:hidden; box-shadow:0 16px 40px rgba(0,0,0,.55); }
            .jellyfix-header { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 18px; background:#202020; border-bottom:1px solid #333; }
            .jellyfix-title { margin:0; color:#00a4dc; font-size:18px; line-height:1.2; }
            .jellyfix-close { border:0; background:transparent; color:#ddd; font-size:28px; line-height:1; cursor:pointer; width:36px; height:36px; }
            .jellyfix-body { padding:18px; overflow:auto; display:flex; flex-direction:column; gap:12px; }
            .jellyfix-footer { display:none; gap:10px; padding:14px; background:#202020; border-top:1px solid #333; }
            .jellyfix-input, .jellyfix-select { width:100%; box-sizing:border-box; border:1px solid #444; background:#101010; color:#fff; border-radius:6px; padding:10px; font:inherit; }
            .jellyfix-input { resize:vertical; min-height:42px; }
            .jellyfix-btn { border:0; border-radius:6px; background:#00a4dc; color:#fff; padding:10px 14px; font-weight:700; cursor:pointer; }
            .jellyfix-btn:disabled { opacity:.55; cursor:not-allowed; }
            .jellyfix-muted { color:#aaa; font-size:13px; }
            .jellyfix-error { color:#ff8a80; }
            .jellyfix-form { display:flex; flex-direction:column; gap:12px; }
            .jellyfix-label { display:flex; flex-direction:column; gap:6px; color:#ddd; }
            .jellyfix-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
            .jellyfix-ticket-row { display:grid; grid-template-columns:1fr auto; gap:10px; align-items:center; border:1px solid #333; border-radius:6px; padding:10px; background:#202020; }
            .jellyfix-status { color:#ccc; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
            .jellyfix-comment { max-width:82%; padding:10px 12px; border-radius:8px; background:#333; align-self:flex-start; white-space:pre-wrap; word-break:break-word; }
            .jellyfix-comment-admin { background:#006f98; align-self:flex-end; }
            .jellyfix-comment-meta { display:block; margin-bottom:5px; color:rgba(255,255,255,.72); font-size:12px; }
            #btn-jellyfix { margin-right:.5em; display:flex; align-items:center; justify-content:center; }
            #btn-admin-tickets { margin-top:5px; }
            .jf-badge-new { color:#ff5252 !important; }
            .jf-badge-work { color:#ffbb33 !important; }
            .jf-badge-ok { color:#00c851 !important; }
        `;
        document.head.appendChild(style);
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function clear(node) {
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    function activeClient() {
        return window.ApiClient || window.ConnectionManager?.currentApiClient?.() || null;
    }

    function readClientValue(client, names) {
        for (const name of names) {
            const value = client && client[name];
            if (typeof value === "function") {
                try {
                    const result = value.call(client);
                    if (result) return result;
                } catch (_err) {}
            } else if (value) {
                return value;
            }
        }
        return null;
    }

    function activeServerId(client) {
        const info = readClientValue(client, ["serverInfo", "_serverInfo"]);
        return info?.Id || info?.ServerId || client?._serverInfo?.Id || client?.serverId || null;
    }

    function tokenFromClient(client) {
        const direct = readClientValue(client, ["accessToken", "getAccessToken", "getCurrentUserToken"]);
        if (typeof direct === "string") return direct;
        const info = readClientValue(client, ["serverInfo", "_serverInfo"]);
        return info?.AccessToken || client?._serverInfo?.AccessToken || null;
    }

    function tokenFromStorage(serverId) {
        if (!serverId) return null;
        try {
            const raw = window.localStorage.getItem("jellyfin_credentials");
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            const servers = Array.isArray(parsed.Servers) ? parsed.Servers : [];
            const match = servers.find((server) => server.Id === serverId || server.ServerId === serverId);
            return match?.AccessToken || null;
        } catch (_err) {
            return null;
        }
    }

    function getToken() {
        const client = activeClient();
        const serverId = activeServerId(client);
        return tokenFromClient(client) || tokenFromStorage(serverId);
    }

    function resetAbort() {
        if (activeAbort) activeAbort.abort();
        activeAbort = new AbortController();
        return activeAbort.signal;
    }

    function invalidateTicketButton() {
        const button = document.getElementById("btn-jellyfix");
        if (button) delete button.dataset.jellyfixLoaded;
    }

    async function api(path, options) {
        const token = getToken();
        if (!token) throw new Error(TEXT.authExpired);
        const init = {
            method: options?.method || "GET",
            headers: {
                "Accept": "application/json",
                "Authorization": `Bearer ${token}`
            },
            signal: options?.signal || activeAbort?.signal
        };
        if (options?.body) {
            init.headers["Content-Type"] = "application/json";
            init.body = JSON.stringify(options.body);
        }
        const response = await fetch(`${API_BASE}${path}`, init);
        if (response.status === 401) throw new Error(TEXT.authExpired);
        if (response.status === 409) {
            const data = await response.json().catch(() => ({}));
            const err = new Error(TEXT.duplicate);
            err.ticketId = data.detail?.ticket_id;
            throw err;
        }
        if (!response.ok) throw new Error(`JellyFix returned ${response.status}`);
        return response.json();
    }

    async function loadMe() {
        if (!meCache) meCache = await api("/me", { signal: resetAbort() });
        return meCache;
    }

    function ensureModal() {
        let overlay = document.getElementById("jellyfix-overlay");
        if (overlay) return overlay;

        overlay = el("div");
        overlay.id = "jellyfix-overlay";
        const modal = el("div", "jellyfix-modal");
        const header = el("div", "jellyfix-header");
        const title = el("h2", "jellyfix-title", TEXT.ticket);
        title.id = "jf-title";
        const close = el("button", "jellyfix-close", "\u00d7");
        close.type = "button";
        close.title = TEXT.close;
        close.addEventListener("click", hideModal);
        const body = el("div", "jellyfix-body");
        body.id = "jf-content";
        const footer = el("div", "jellyfix-footer");
        footer.id = "jf-footer";
        header.append(title, close);
        modal.append(header, body, footer);
        overlay.appendChild(modal);
        document.body.appendChild(overlay);
        return overlay;
    }

    function showModal(title) {
        const overlay = ensureModal();
        document.getElementById("jf-title").textContent = title;
        clear(document.getElementById("jf-content"));
        clear(document.getElementById("jf-footer"));
        document.getElementById("jf-footer").style.display = "none";
        overlay.style.display = "flex";
    }

    function hideModal() {
        resetAbort();
        invalidateTicketButton();
        const overlay = document.getElementById("jellyfix-overlay");
        if (overlay) overlay.style.display = "none";
        refreshButton();
    }

    function showError(message) {
        const content = document.getElementById("jf-content");
        clear(content);
        content.appendChild(el("div", "jellyfix-error", message || TEXT.loadError));
    }

    function currentPageItemId() {
        const hash = window.location.hash || "";
        const query = hash.includes("?") ? hash.slice(hash.indexOf("?") + 1) : "";
        const id = new URLSearchParams(query).get("id");
        return id || null;
    }

    async function showCreateForm(itemId) {
        currentItemId = itemId;
        showModal(TEXT.report);
        const content = document.getElementById("jf-content");
        const form = el("form", "jellyfix-form");
        const issueLabel = el("label", "jellyfix-label", TEXT.issueType);
        const select = el("select", "jellyfix-select");
        for (const [value, label] of ISSUE_TYPES) {
            const option = el("option", null, label);
            option.value = value;
            select.appendChild(option);
        }
        issueLabel.appendChild(select);
        const messageLabel = el("label", "jellyfix-label", TEXT.details);
        const textarea = el("textarea", "jellyfix-input");
        textarea.maxLength = 2000;
        textarea.rows = 5;
        textarea.placeholder = TEXT.messagePlaceholder;
        messageLabel.appendChild(textarea);
        const submit = el("button", "jellyfix-btn", TEXT.send);
        submit.type = "submit";
        form.append(issueLabel, messageLabel, submit);
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!textarea.value.trim()) return;
            submit.disabled = true;
            try {
                const created = await api("/tickets", {
                    method: "POST",
                    body: {
                        item_id: currentItemId,
                        issue_type: select.value,
                        message: textarea.value.trim()
                    },
                    signal: resetAbort()
                });
                await showTicket(created.id);
            } catch (err) {
                if (err.ticketId) await showTicket(err.ticketId);
                else showError(err.message);
            } finally {
                submit.disabled = false;
            }
        });
        content.appendChild(form);
    }

    function appendStatusControls(content, ticket) {
        if (!meCache?.is_admin) return;
        const row = el("div", "jellyfix-row");
        const actions = [];
        if (ticket.status === "new") actions.push(["in_progress", TEXT.inProgress], ["resolved", TEXT.resolve]);
        if (ticket.status === "in_progress") actions.push(["resolved", TEXT.resolve]);
        if (ticket.status === "resolved") actions.push(["in_progress", TEXT.reopen]);
        for (const [status, label] of actions) {
            const button = el("button", "jellyfix-btn", label);
            button.type = "button";
            button.addEventListener("click", async () => {
                button.disabled = true;
                try {
                    await api(`/tickets/${ticket.id}/status`, {
                        method: "PATCH",
                        body: { status },
                        signal: resetAbort()
                    });
                    invalidateTicketButton();
                    await showTicket(ticket.id);
                } catch (err) {
                    showError(err.message);
                }
            });
            row.appendChild(button);
        }
        if (actions.length > 0) content.appendChild(row);
    }

    async function showTicket(ticketId) {
        showModal(TEXT.ticket);
        try {
            await loadMe();
            const data = await api(`/tickets/${ticketId}`, { signal: resetAbort() });
            const ticket = data.ticket;
            const comments = data.comments || [];
            const title = document.getElementById("jf-title");
            title.textContent = `${TEXT.ticket}: ${ticket.item_name}`;
            const content = document.getElementById("jf-content");
            clear(content);
            const status = el("div", "jellyfix-status", ticket.status);
            content.appendChild(status);
            appendStatusControls(content, ticket);
            if (ticket.status === "resolved" && !meCache.is_admin) {
                content.appendChild(el("div", "jellyfix-muted", TEXT.resolved));
            }
            for (const comment of comments) {
                const bubble = el("div", comment.is_admin ? "jellyfix-comment jellyfix-comment-admin" : "jellyfix-comment");
                const meta = el("span", "jellyfix-comment-meta", `${comment.author_name} - ${new Date(comment.created_at).toLocaleString()}`);
                const msg = el("span", null, comment.message);
                bubble.append(meta, msg);
                content.appendChild(bubble);
            }
            content.scrollTop = content.scrollHeight;
            renderCommentFooter(ticket);
        } catch (err) {
            showError(err.message);
        }
    }

    function renderCommentFooter(ticket) {
        const footer = document.getElementById("jf-footer");
        clear(footer);
        if (ticket.status === "resolved" && !meCache?.is_admin) {
            footer.style.display = "none";
            return;
        }
        const input = el("textarea", "jellyfix-input");
        input.maxLength = 2000;
        input.placeholder = TEXT.messagePlaceholder;
        const send = el("button", "jellyfix-btn", TEXT.send);
        send.type = "button";
        send.addEventListener("click", async () => {
            const message = input.value.trim();
            if (!message) return;
            send.disabled = true;
            try {
                await api(`/tickets/${ticket.id}/comments`, {
                    method: "POST",
                    body: { message },
                    signal: resetAbort()
                });
                invalidateTicketButton();
                await showTicket(ticket.id);
            } catch (err) {
                showError(err.message);
            }
        });
        footer.append(input, send);
        footer.style.display = "flex";
    }

    async function showManager() {
        showModal(TEXT.manager);
        try {
            await loadMe();
            if (!meCache.is_admin) throw new Error("Admin required");
            const data = await api("/admin/tickets?limit=50", { signal: resetAbort() });
            const content = document.getElementById("jf-content");
            clear(content);
            if (!data.tickets || data.tickets.length === 0) {
                content.appendChild(el("div", "jellyfix-muted", TEXT.noTickets));
                return;
            }
            for (const ticket of data.tickets) {
                const row = el("div", "jellyfix-ticket-row");
                const info = el("div");
                info.appendChild(el("div", null, ticket.item_name));
                info.appendChild(el("div", "jellyfix-muted", `${ticket.reporter_name} - ${ticket.issue_type}`));
                info.appendChild(el("div", "jellyfix-status", ticket.status));
                const open = el("button", "jellyfix-btn", "Open");
                open.type = "button";
                open.addEventListener("click", () => showTicket(ticket.id));
                row.append(info, open);
                content.appendChild(row);
            }
        } catch (err) {
            showError(err.message);
        }
    }

    async function injectAdminMenu() {
        try {
            await loadMe();
        } catch (_err) {
            return;
        }
        const existing = document.getElementById("btn-admin-tickets");
        if (!meCache?.is_admin) {
            if (existing) existing.remove();
            return;
        }
        if (existing) return;
        const link = document.createElement("button");
        link.id = "btn-admin-tickets";
        link.type = "button";
        link.className = "navMenuOption emby-button";
        const icon = el("span", "navMenuOptionIcon material-icons", "build");
        icon.setAttribute("aria-hidden", "true");
        const text = el("span", "navMenuOptionText", TEXT.manager);
        link.append(icon, text);
        link.addEventListener("click", showManager);
        const dashboardLink = document.querySelector('a[href*="dashboard"]');
        if (dashboardLink?.parentNode) dashboardLink.parentNode.insertBefore(link, dashboardLink.nextSibling);
        else (document.querySelector(".mainDrawer-content") || document.querySelector(".mainDrawer-scrollContainer"))?.appendChild(link);
    }

    function statusIcon(status) {
        if (status === "new") return ["error", "jf-badge-new"];
        if (status === "in_progress") return ["build", "jf-badge-work"];
        if (status === "resolved") return ["check_circle", "jf-badge-ok"];
        return ["flag", ""];
    }

    function renderTicketButton(button, itemId, ticket) {
        const state = ticket?.status || "none";
        const stateKey = `${itemId}:${state}:${ticket?.id || ""}`;
        if (button.dataset.jellyfixState === stateKey) return;

        clear(button);
        const [iconName, iconClass] = statusIcon(ticket?.status);
        const icon = el("span", `material-icons detailButton-icon ${iconClass}`.trim(), iconName);
        button.appendChild(icon);
        button.dataset.itemId = itemId;
        button.dataset.ticketId = ticket?.id || "";
        button.dataset.jellyfixState = stateKey;
        button.title = ticket ? `${TEXT.ticket}: ${ticket.status}` : TEXT.report;
    }

    function ensureTicketButton(container, itemId) {
        let button = document.getElementById("btn-jellyfix");
        if (button && button.parentElement !== container) button.remove();
        button = document.getElementById("btn-jellyfix");
        if (button) return button;

        button = document.createElement("button");
        button.id = "btn-jellyfix";
        button.type = "button";
        button.className = "itemsDetailButton emby-button button-flat detailButton";
        button.addEventListener("click", () => {
            const ticketId = button.dataset.ticketId;
            if (ticketId) showTicket(ticketId);
            else showCreateForm(button.dataset.itemId || itemId);
        });
        renderTicketButton(button, itemId, null);

        const buttons = Array.from(container.querySelectorAll("button"));
        const moreButton = buttons.find((candidate) => candidate.textContent.includes("more_vert"));
        if (moreButton) container.insertBefore(button, moreButton);
        else container.appendChild(button);
        return button;
    }

    async function refreshButton() {
        if (refreshInFlight) return;
        await injectAdminMenu();
        const container = document.querySelector(".mainDetailButtons");
        if (!container) return;
        const itemId = currentPageItemId();
        if (!itemId) return;
        currentItemId = itemId;
        const button = ensureTicketButton(container, itemId);
        if (button.dataset.itemId === itemId && button.dataset.jellyfixLoaded === "true") return;

        try {
            refreshInFlight = true;
            let ticket = null;
            const data = await api(`/items/${itemId}/ticket`, { signal: activeAbort?.signal });
            ticket = data.ticket;
            if (currentPageItemId() === itemId && button.isConnected) {
                renderTicketButton(button, itemId, ticket);
                button.dataset.jellyfixLoaded = "true";
            }
        } catch (_err) {}
        finally {
            refreshInFlight = false;
        }
    }

    function scheduleRefresh() {
        window.clearTimeout(observerTimer);
        observerTimer = window.setTimeout(refreshButton, 350);
    }

    addStyles();
    ensureModal();
    let lastHref = window.location.href;
    const observer = new MutationObserver(() => {
        if (window.location.href !== lastHref) {
            lastHref = window.location.href;
            resetAbort();
            currentItemId = null;
        }
        if (document.querySelector(".mainDetailButtons") || document.querySelector(".mainDrawer-content")) {
            scheduleRefresh();
        }
    });
    observer.observe(document, { childList: true, subtree: true });
    window.setTimeout(refreshButton, 800);
})();
