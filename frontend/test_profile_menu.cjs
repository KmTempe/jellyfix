const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");

// Exercise the menu's authorization and lifecycle without a live Jellyfin account.
const source = readFileSync(`${__dirname}/injector.js`, "utf8");
const menuSource = source.slice(source.indexOf("    const checkedProfileMenus"), source.indexOf("    function statusIcon"));

function fixture() {
    const state = { token: "admin-session", user: { is_admin: true }, calls: 0, opened: [], nodes: [] };
    function element(tag, className, text) {
        return {
            tag, className, text, style: {}, listeners: {}, children: [],
            setAttribute(key, value) { this[key] = value; },
            append(...children) { this.children.push(...children); },
            addEventListener(name, callback) { this.listeners[name] = callback; },
            dispatchEvent(event) { state.dismissed = event.key === "Escape"; },
            click() { return this.listeners.click({ preventDefault() {} }); },
            after(node) { state.nodes.splice(state.nodes.indexOf(this) + 1, 0, node); },
            remove() { state.nodes = state.nodes.filter(node => node !== this); }
        };
    }
    state.mount = (sidebar = false, dashboard = false) => {
        state.sidebar = sidebar;
        state.menu = {
            isConnected: true,
            appendChild(node) { state.nodes.push(node); },
            querySelector: selector => selector.includes("dashboard") ?
                (dashboard ? state.settings : null) : state.nodes.find(node => `#${node.id}` === selector)
        };
        state.settings = element("a", "MuiMenuItem-root", "Settings");
        state.settings.closest = () => state.menu;
        state.nodes = [state.settings];
    };
    state.mount();
    const context = vm.createContext({
        document: {
            querySelector: selector => selector.includes('role="menu"') ?
                (state.sidebar ? null : state.settings) : (state.sidebar ? state.menu : null),
            getElementById: id => state.nodes.find(node => node.id === id)
        },
        getToken: () => state.token,
        api: async () => { state.calls++; return state.response ? state.response() : state.user; },
        el: element, meCache: null, AbortController,
        KeyboardEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
        TEXT: { history: "My Ticket History", manager: "Ticket Manager" },
        showHistory: () => state.opened.push("history"),
        showManager: () => state.opened.push("manager")
    });
    vm.runInContext(menuSource, context);
    state.inject = () => context.injectProfileMenu();
    return state;
}

test("admin menu inserts both items once, and checks again on remount", async () => {
    const state = fixture();
    await state.inject();
    await state.inject();
    assert.deepEqual(state.nodes.map(node => node.id), [undefined, "btn-jellyfix-history", "btn-admin-tickets"]);
    assert.equal(state.calls, 1);
    assert.equal(state.nodes[1].role, "menuitem");
    assert.equal(state.nodes[1].tabIndex, -1);
    state.mount();
    await state.inject();
    assert.equal(state.calls, 2);
    assert.equal(state.nodes.length, 3);
});

test("non-admin users get history but not the manager", async () => {
    const state = fixture();
    state.response = () => ({ is_admin: false });
    await state.inject();
    assert.deepEqual(state.nodes.map(node => node.id), [undefined, "btn-jellyfix-history"]);
    const failed = fixture();
    failed.response = () => { throw new Error("unavailable"); };
    await failed.inject();
    assert.equal(failed.nodes.length, 1);
});

test("late authorization cannot populate a detached menu or a different session", async () => {
    for (const changeSession of [false, true]) {
        const state = fixture();
        let finish;
        state.response = () => new Promise(resolve => { finish = resolve; });
        const pending = state.inject();
        if (changeSession) state.token = "member-session";
        else state.menu.isConnected = false;
        finish({ is_admin: true });
        await pending;
        assert.equal(state.nodes.length, 1);
    }
});

test("click rechecks privileges and closes the menu before opening the dialog", async () => {
    const state = fixture();
    await state.inject();
    await state.nodes[1].click();
    assert.equal(state.dismissed, true);
    assert.deepEqual(state.opened, ["history"]);
    state.user = { is_admin: false };
    await state.nodes[2].click();
    assert.deepEqual(state.opened, ["history"]);
    assert.equal(state.nodes.length, 1);
});

test("Enter and Space activate menu entries", async () => {
    const state = fixture();
    await state.inject();
    let clicks = 0;
    state.nodes[1].click = () => { clicks++; };
    for (const key of ["Enter", " "]) {
        let prevented = false;
        state.nodes[1].listeners.keydown({ key, preventDefault() { prevented = true; } });
        assert.equal(prevented, true);
    }
    assert.equal(clicks, 2);
});

test("legacy sidebar supports placement with and without Dashboard", async () => {
    for (const dashboard of [false, true]) {
        const state = fixture();
        state.mount(true, dashboard);
        await state.inject();
        await state.inject();
        assert.equal(state.nodes.length, 3);
        assert.equal(state.nodes[1].className, "navMenuOption");
        assert.equal(state.nodes[1].role, "button");
        assert.equal(state.nodes[1].tabIndex, 0);
        await state.nodes[2].click();
        assert.deepEqual(state.opened, ["manager"]);
    }
});

test("persistent sidebar removes manager when the session changes to a member", async () => {
    const state = fixture();
    state.mount(true);
    await state.inject();
    state.token = "member-session";
    state.user = { is_admin: false };
    await state.inject();
    assert.equal(state.nodes.length, 2);
    state.token = "another-admin-session";
    state.user = { is_admin: true };
    await state.inject();
    assert.equal(state.nodes.length, 3);
    state.token = null;
    await state.inject();
    assert.equal(state.nodes.length, 1);
});
