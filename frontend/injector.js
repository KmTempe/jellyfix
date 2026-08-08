/*
 * JellyFix secure injector for Jellyfin Web.
 * Install this once in Jellyfin Web and serve the backend at /jellyfix.
 */
(function () {
    "use strict";

    const API_BASE = `${window.location.origin}/jellyfix/api/v1`;
    const TICKET_POLL_INTERVAL_MS = 5000;
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
        history: "My Ticket History",
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
        cooldown: "This ticket was recently resolved. You can create a new report in {time}.",
        loadMore: "Load more",
        inProgress: "Set in progress",
        resolve: "Resolve",
        reopen: "Reopen",
        delete: "Delete",
        selectAll: "Select all",
        deleteSelected: "Delete selected",
        deleteConfirm: "Delete this ticket permanently? This cannot be undone.",
        deleteSelectedConfirm: "Delete the selected tickets permanently? This cannot be undone.",
        updateStatus: "Update status",
        chooseStatus: "Choose status",
        setInProgress: "Set in progress / reopen",
        deleteActiveDisabled: "Resolve this ticket before deleting it.",
        statusNew: "New",
        statusProgress: "In progress",
        statusResolved: "Resolved"
    };

    let activeAbort = new AbortController();
    let currentItemId = null;
    let meCache = null;
    let observerTimer = null;
    let refreshRetryTimer = null;
    let refreshRetryCount = 0;
    let refreshInFlight = false;
    let openTicket = null;
    let ticketPollTimer = null;
    let ticketPollAbort = null;
    let ticketPollInFlight = false;
    let cooldownTimer = null;

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
            .jellyfix-ticket-actions { display:flex; align-items:center; gap:8px; }
            .jellyfix-ticket-select { width:18px; height:18px; }
            .jellyfix-danger { background:#c62828; }
            .jellyfix-status { color:#ccc; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
            .jellyfix-comments { display:flex; flex-direction:column; gap:12px; }
            .jellyfix-comment { max-width:82%; padding:10px 12px; border-radius:8px; background:#333; align-self:flex-start; white-space:pre-wrap; word-break:break-word; }
            .jellyfix-comment-admin { background:#006f98; align-self:flex-start; }
            .jellyfix-comment-reporter { align-self:flex-end; }
            .jellyfix-comment-system { background:#4c4c4c; align-self:center; max-width:90%; }
            .jellyfix-comment-meta { display:block; margin-bottom:5px; color:rgba(255,255,255,.72); font-size:12px; }
            .jellyfix-comment-delivery { display:block; margin-top:6px; color:#ffd166; font-size:12px; }
            .jellyfix-comment-delivery-error { color:#ff8a80; }
            .jellyfix-csat { border:1px solid #38bdf8; background:#075985; }
            .jellyfix-csat-title { display:block; margin-bottom:6px; font-weight:600; }
            .jellyfix-csat-action { display:block; width:max-content; margin-top:10px; color:#fff; background:#0284c7; border-radius:5px; padding:7px 10px; text-decoration:none; }
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

    function stopTicketPolling() {
        if (ticketPollTimer) window.clearInterval(ticketPollTimer);
        ticketPollTimer = null;
        if (ticketPollAbort) ticketPollAbort.abort();
        ticketPollAbort = null;
        ticketPollInFlight = false;
        openTicket = null;
    }

    function scheduleCooldownRefresh(ticket) {
        if (cooldownTimer) window.clearTimeout(cooldownTimer);
        cooldownTimer = null;
        if (!ticket?.cooldown_expires_at || ticket.status !== "resolved") return;
        const delay = new Date(ticket.cooldown_expires_at).getTime() - Date.now();
        if (!Number.isFinite(delay)) return;
        cooldownTimer = window.setTimeout(() => {
            invalidateTicketButton();
            refreshButton();
        }, Math.max(delay + 100, 100));
    }

    function cooldownMessage(expiresAt) {
        const seconds = Math.max(0, Math.ceil((new Date(expiresAt).getTime() - Date.now()) / 1000));
        return TEXT.cooldown.replace("{time}", `${seconds}s`);
    }

    function ticketModalIsOpen(ticketId) {
        const overlay = document.getElementById("jellyfix-overlay");
        return overlay?.style.display === "flex" && openTicket?.id === ticketId;
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
            const detail = data.detail;
            const err = new Error(
                typeof detail === "object" && detail?.message
                    ? detail.message
                    : typeof detail === "string"
                        ? detail
                        : TEXT.duplicate
            );
            err.ticketId = detail?.ticket_id;
            throw err;
        }
        if (response.status === 429) {
            const data = await response.json().catch(() => ({}));
            const detail = data.detail;
            const err = new Error(
                typeof detail === "object" && detail?.message ? detail.message : `JellyFix returned 429`
            );
            err.ticketId = detail?.ticket_id;
            err.cooldownExpiresAt = detail?.cooldown_expires_at;
            err.retryAfter = Number(response.headers.get("Retry-After") || detail?.retry_after || 0);
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
        stopTicketPolling();
        const overlay = ensureModal();
        document.getElementById("jf-title").textContent = title;
        clear(document.getElementById("jf-content"));
        clear(document.getElementById("jf-footer"));
        document.getElementById("jf-footer").style.display = "none";
        overlay.style.display = "flex";
    }

    function hideModal() {
        stopTicketPolling();
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

    function commentBubble(comment) {
        const role = comment.author_role || (comment.is_admin ? "agent" : "reporter");
        const bubbleClass = role === "agent"
            ? "jellyfix-comment jellyfix-comment-admin"
            : role === "system" ? "jellyfix-comment jellyfix-comment-system" : "jellyfix-comment jellyfix-comment-reporter";
        const metadata = comment.metadata && typeof comment.metadata === "object" ? comment.metadata : {};
        const isCsat = metadata.kind === "csat";
        const bubble = el("div", `${bubbleClass}${isCsat ? " jellyfix-csat" : ""}`);
        const roleLabel = role === "agent" ? "🎧 Support agent" : role === "system" ? "⚙ System" : "👤 Reporter";
        const meta = el("span", "jellyfix-comment-meta", `${roleLabel}: ${comment.author_name} - ${new Date(comment.created_at).toLocaleString()}`);
        const msg = el("span", null, comment.message);
        bubble.dataset.commentId = comment.id;
        if (isCsat) {
            const title = el("span", "jellyfix-csat-title", "Rate this support");
            bubble.append(meta, title, msg);
            const action = Array.isArray(metadata.actions) ? metadata.actions[0] : null;
            if (action && typeof action.url === "string" && /^https?:\/\//i.test(action.url)) {
                const link = el("a", "jellyfix-csat-action", typeof action.label === "string" ? action.label : "Rate this support");
                link.href = action.url;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                bubble.appendChild(link);
            }
        } else {
            bubble.append(meta, msg);
        }
        if (comment.delivery_status === "pending" || comment.delivery_status === "queued" || comment.delivery_status === "error") {
            const statusText = comment.delivery_status === "error" ? "Delivery retry scheduled" : "Queued for delivery";
            bubble.appendChild(el("span", `jellyfix-comment-delivery${comment.delivery_status === "error" ? " jellyfix-comment-delivery-error" : ""}`, statusText));
        }
        return bubble;
    }

    function updateTicketStatus(ticket) {
        const status = document.getElementById("jf-ticket-status");
        if (status) status.textContent = ticket.status;

        const controls = document.getElementById("jf-status-controls");
        if (controls) {
            clear(controls);
            appendStatusControls(controls, ticket);
        }

        const resolvedNote = document.getElementById("jf-resolved-note");
        if (resolvedNote) {
            clear(resolvedNote);
            if (ticket.status === "resolved" && !meCache?.is_admin) {
                const message = ticket.cooldown_expires_at
                    ? cooldownMessage(ticket.cooldown_expires_at)
                    : TEXT.resolved;
                resolvedNote.appendChild(el("div", "jellyfix-muted", message));
            }
        }
        scheduleCooldownRefresh(ticket);
        renderCommentFooter(ticket);
    }

    function renderTicket(ticket, comments) {
        const title = document.getElementById("jf-title");
        title.textContent = `${TEXT.ticket}: ${ticket.item_name}`;
        const content = document.getElementById("jf-content");
        clear(content);

        const status = el("div", "jellyfix-status", ticket.status);
        status.id = "jf-ticket-status";
        const controls = el("div");
        controls.id = "jf-status-controls";
        const resolvedNote = el("div");
        resolvedNote.id = "jf-resolved-note";
        const commentsContainer = el("div", "jellyfix-comments");
        commentsContainer.id = "jf-comments";
        for (const comment of comments) commentsContainer.appendChild(commentBubble(comment));
        content.append(status, controls, resolvedNote, commentsContainer);

        openTicket = {
            id: ticket.id,
            status: ticket.status,
            commentIds: new Set(comments.map((comment) => comment.id))
        };
        updateTicketStatus(ticket);
        content.scrollTop = content.scrollHeight;
    }

    async function refreshOpenTicket(ticketId) {
        if (ticketPollInFlight || document.hidden || !ticketModalIsOpen(ticketId)) return;
        ticketPollInFlight = true;
        ticketPollAbort = new AbortController();
        try {
            const data = await api(`/tickets/${ticketId}`, { signal: ticketPollAbort.signal });
            if (!ticketModalIsOpen(ticketId)) return;

            const ticket = data.ticket;
            const comments = data.comments || [];
            const previous = openTicket;
            if (ticket.status !== previous.status) {
                previous.status = ticket.status;
                updateTicketStatus(ticket);
                invalidateTicketButton();
            }

            const commentsContainer = document.getElementById("jf-comments");
            const content = document.getElementById("jf-content");
            const wasAtBottom = content && content.scrollHeight - content.scrollTop - content.clientHeight < 48;
            let addedComment = false;
            for (const comment of comments) {
                if (!previous.commentIds.has(comment.id) && commentsContainer) {
                    commentsContainer.appendChild(commentBubble(comment));
                    previous.commentIds.add(comment.id);
                    addedComment = true;
                }
            }
            if (addedComment && wasAtBottom && content) content.scrollTop = content.scrollHeight;
        } catch (_err) {
            // A transient poll failure must not disrupt an open ticket or the user's draft reply.
        } finally {
            ticketPollInFlight = false;
            ticketPollAbort = null;
        }
    }

    function startTicketPolling(ticketId) {
        ticketPollTimer = window.setInterval(() => refreshOpenTicket(ticketId), TICKET_POLL_INTERVAL_MS);
    }

    async function showTicket(ticketId) {
        showModal(TEXT.ticket);
        try {
            await loadMe();
            const data = await api(`/tickets/${ticketId}`, { signal: resetAbort() });
            const ticket = data.ticket;
            renderTicket(ticket, data.comments || []);
            startTicketPolling(ticketId);
        } catch (err) {
            showError(err.message);
        }
    }

    async function deleteTickets(ticketIds) {
        await api("/tickets", {
            method: "DELETE",
            body: { ticket_ids: ticketIds },
            signal: resetAbort()
        });
        invalidateTicketButton();
    }

    async function updateTicketsStatus(ticketIds, status) {
        await api("/tickets/status", {
            method: "PATCH",
            body: { ticket_ids: ticketIds, status },
            signal: resetAbort()
        });
        invalidateTicketButton();
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
            const selectedIds = new Set();
            const checkboxes = [];
            const ticketById = new Map(data.tickets.map((ticket) => [ticket.id, ticket]));
            const toolbar = el("div", "jellyfix-row");
            const selectAll = el("input", "jellyfix-ticket-select");
            selectAll.type = "checkbox";
            selectAll.id = "jf-select-all";
            const selectAllLabel = el("label", null, TEXT.selectAll);
            selectAllLabel.htmlFor = selectAll.id;
            const deleteSelected = el("button", "jellyfix-btn jellyfix-danger", TEXT.deleteSelected);
            deleteSelected.type = "button";
            deleteSelected.disabled = true;
            const statusSelect = el("select", "jellyfix-select");
            statusSelect.style.width = "auto";
            const statusPlaceholder = el("option", null, TEXT.chooseStatus);
            statusPlaceholder.value = "";
            statusPlaceholder.disabled = true;
            statusPlaceholder.selected = true;
            const inProgress = el("option", null, TEXT.setInProgress);
            inProgress.value = "in_progress";
            const resolved = el("option", null, TEXT.resolve);
            resolved.value = "resolved";
            statusSelect.append(statusPlaceholder, inProgress, resolved);
            const updateStatus = el("button", "jellyfix-btn", TEXT.updateStatus);
            updateStatus.type = "button";
            updateStatus.disabled = true;
            const updateSelection = () => {
                const selectedCount = selectedIds.size;
                const containsActiveTicket = Array.from(selectedIds).some(
                    (ticketId) => ticketById.get(ticketId)?.status !== "resolved"
                );
                deleteSelected.disabled = selectedCount === 0 || (
                    containsActiveTicket && !meCache?.allow_active_ticket_deletion
                );
                deleteSelected.title = deleteSelected.disabled && containsActiveTicket
                    ? TEXT.deleteActiveDisabled
                    : "";
                updateStatus.disabled = selectedCount === 0 || !statusSelect.value;
                selectAll.checked = checkboxes.length > 0 && selectedCount === checkboxes.length;
                selectAll.indeterminate = selectedCount > 0 && selectedCount < checkboxes.length;
            };
            selectAll.addEventListener("change", () => {
                for (const checkbox of checkboxes) {
                    checkbox.checked = selectAll.checked;
                    if (checkbox.checked) selectedIds.add(checkbox.dataset.ticketId);
                    else selectedIds.delete(checkbox.dataset.ticketId);
                }
                updateSelection();
            });
            statusSelect.addEventListener("change", updateSelection);
            deleteSelected.addEventListener("click", async () => {
                if (!selectedIds.size || !window.confirm(TEXT.deleteSelectedConfirm)) return;
                deleteSelected.disabled = true;
                try {
                    await deleteTickets(Array.from(selectedIds));
                    await showManager();
                } catch (err) {
                    showError(err.message);
                }
            });
            updateStatus.addEventListener("click", async () => {
                const status = statusSelect.value;
                if (!selectedIds.size || !status) return;
                updateStatus.disabled = true;
                try {
                    await updateTicketsStatus(Array.from(selectedIds), status);
                    await showManager();
                } catch (err) {
                    showError(err.message);
                }
            });
            toolbar.append(selectAll, selectAllLabel, statusSelect, updateStatus, deleteSelected);
            content.appendChild(toolbar);
            for (const ticket of data.tickets) {
                const row = el("div", "jellyfix-ticket-row");
                const info = el("div");
                info.appendChild(el("div", null, ticket.item_name));
                info.appendChild(el("div", "jellyfix-muted", `${ticket.reporter_name} - ${ticket.issue_type}`));
                info.appendChild(el("div", "jellyfix-status", ticket.status));
                const actions = el("div", "jellyfix-ticket-actions");
                const select = el("input", "jellyfix-ticket-select");
                select.type = "checkbox";
                select.dataset.ticketId = ticket.id;
                select.setAttribute("aria-label", `Select ${ticket.item_name}`);
                select.addEventListener("change", () => {
                    if (select.checked) selectedIds.add(ticket.id);
                    else selectedIds.delete(ticket.id);
                    updateSelection();
                });
                checkboxes.push(select);
                const open = el("button", "jellyfix-btn", "Open");
                open.type = "button";
                open.addEventListener("click", () => showTicket(ticket.id));
                const remove = el("button", "jellyfix-btn jellyfix-danger", TEXT.delete);
                remove.type = "button";
                if (ticket.status !== "resolved" && !meCache?.allow_active_ticket_deletion) {
                    remove.disabled = true;
                    remove.title = TEXT.deleteActiveDisabled;
                }
                remove.addEventListener("click", async () => {
                    if (!window.confirm(TEXT.deleteConfirm)) return;
                    remove.disabled = true;
                    try {
                        await deleteTickets([ticket.id]);
                        await showManager();
                    } catch (err) {
                        showError(err.message);
                    }
                });
                actions.append(select, open, remove);
                row.append(info, actions);
                content.appendChild(row);
            }
        } catch (err) {
            showError(err.message);
        }
    }

    async function showHistory() {
        showModal(TEXT.history);
        try {
            await loadMe();
            const content = document.getElementById("jf-content");
            let cursor = null;
            let loading = false;
            const loadPage = async () => {
                if (loading) return;
                loading = true;
                try {
                    const suffix = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
                    const data = await api(`/tickets/mine?limit=50${suffix}`, { signal: resetAbort() });
                    if (!data.tickets?.length && !cursor) {
                        content.appendChild(el("div", "jellyfix-muted", TEXT.noTickets));
                        return;
                    }
                    for (const ticket of data.tickets || []) {
                        const row = el("div", "jellyfix-ticket-row");
                        const info = el("div");
                        info.appendChild(el("div", null, ticket.item_name));
                        info.appendChild(el("div", "jellyfix-muted", ticket.issue_type));
                        info.appendChild(el("div", "jellyfix-status", ticket.status));
                        const open = el("button", "jellyfix-btn", "Open");
                        open.type = "button";
                        open.addEventListener("click", () => showTicket(ticket.id));
                        row.append(info, open);
                        content.appendChild(row);
                    }
                    cursor = data.next_cursor || null;
                    const previousMore = document.getElementById("jf-history-more");
                    if (previousMore) previousMore.remove();
                    if (cursor) {
                        const more = el("button", "jellyfix-btn", TEXT.loadMore);
                        more.id = "jf-history-more";
                        more.type = "button";
                        more.addEventListener("click", loadPage);
                        content.appendChild(more);
                    }
                } finally {
                    loading = false;
                }
            };
            await loadPage();
        } catch (err) {
            showError(err.message);
        }
    }

    async function injectHistoryMenu() {
        try {
            await loadMe();
        } catch (_err) {
            return;
        }
        if (document.getElementById("btn-jellyfix-history")) return;
        const link = document.createElement("a");
        link.id = "btn-jellyfix-history";
        link.href = "#";
        link.setAttribute("role", "button");
        link.className = "navMenuOption";
        const icon = el("span", "navMenuOptionIcon material-icons", "history");
        icon.setAttribute("aria-hidden", "true");
        const text = el("span", "navMenuOptionText", TEXT.history);
        link.append(icon, text);
        link.addEventListener("click", (event) => {
            event.preventDefault();
            showHistory();
        });
        const dashboardLink = document.querySelector('a[href*="dashboard"]');
        if (dashboardLink?.parentNode) dashboardLink.parentNode.insertBefore(link, dashboardLink.nextSibling);
        else (document.querySelector(".mainDrawer-content") || document.querySelector(".mainDrawer-scrollContainer"))?.appendChild(link);
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
        const link = document.createElement("a");
        link.id = "btn-admin-tickets";
        link.href = "#";
        link.setAttribute("role", "button");
        link.className = "navMenuOption";
        const icon = el("span", "navMenuOptionIcon material-icons", "build");
        icon.setAttribute("aria-hidden", "true");
        const text = el("span", "navMenuOptionText", TEXT.manager);
        link.append(icon, text);
        link.addEventListener("click", (event) => {
            event.preventDefault();
            showManager();
        });
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
        await injectHistoryMenu();
        await injectAdminMenu();
        const container = document.querySelector(".mainDetailButtons");
        if (!container) {
            scheduleRefreshRetry();
            return;
        }
        const itemId = currentPageItemId();
        if (!itemId) {
            scheduleRefreshRetry();
            return;
        }
        currentItemId = itemId;
        const button = ensureTicketButton(container, itemId);
        if (button.dataset.itemId === itemId && button.dataset.jellyfixLoaded === "true") {
            refreshRetryCount = 0;
            return;
        }

        try {
            refreshInFlight = true;
            let ticket = null;
            const data = await api(`/items/${itemId}/ticket`, { signal: activeAbort?.signal });
            ticket = data.ticket;
            scheduleCooldownRefresh(ticket);
            if (currentPageItemId() === itemId && button.isConnected) {
                renderTicketButton(button, itemId, ticket);
                button.dataset.jellyfixLoaded = "true";
                refreshRetryCount = 0;
            }
        } catch (_err) {}
        finally {
            refreshInFlight = false;
        }
    }

    function scheduleRefreshRetry() {
        if (!window.location.hash.includes("#/details?")) return;
        if (refreshRetryTimer || refreshRetryCount >= 15) return;
        refreshRetryCount += 1;
        refreshRetryTimer = window.setTimeout(() => {
            refreshRetryTimer = null;
            refreshButton();
        }, 300);
    }

    function scheduleRefresh() {
        window.clearTimeout(observerTimer);
        if (refreshRetryTimer) {
            window.clearTimeout(refreshRetryTimer);
            refreshRetryTimer = null;
        }
        refreshRetryCount = 0;
        observerTimer = window.setTimeout(refreshButton, 350);
    }

    addStyles();
    ensureModal();
    let lastHref = window.location.href;
    const observer = new MutationObserver(() => {
        if (window.location.href !== lastHref) {
            lastHref = window.location.href;
            resetAbort();
            stopTicketPolling();
            currentItemId = null;
            refreshRetryCount = 0;
        }
        if (document.querySelector(".mainDetailButtons") || document.querySelector(".mainDrawer-content")) {
            scheduleRefresh();
        }
    });
    observer.observe(document, { childList: true, subtree: true });
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden && openTicket) refreshOpenTicket(openTicket.id);
    });
    window.setTimeout(refreshButton, 800);
})();
