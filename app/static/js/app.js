// App shell: auth state, conversation sidebar, header (model, quota), wiring of the panes.
import { api, onNetworkChange, resetMessage } from "./api.js";
import { Chat } from "./chat.js";
import { clear, formatNumber, h, toast } from "./dom.js";
import { DatasetsPanel, SettingsPanel } from "./panels.js";

const $ = (id) => document.getElementById(id);

const state = { conversations: [], nextCursor: null, current: null, allowedModels: [] };

function title(conversation) {
  return conversation.title || "New chat";
}

// ---- usage meter ----------------------------------------------------------------------------

function showUsage({ used, limit }) {
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  $("usage-bar").style.width = `${pct}%`;
  $("usage-bar").classList.toggle("full", pct >= 100);
  $("usage-text").textContent = `${formatNumber(used)} / ${formatNumber(limit)} tokens today`;
}

async function refreshUsage() {
  try {
    const usage = await api("/usage");
    showUsage(usage);
    $("usage").title = `Resets at ${resetMessage((new Date(usage.reset_at) - Date.now()) / 1000)}`;
  } catch {
    /* the meter is informational */
  }
}

// ---- conversations --------------------------------------------------------------------------

function drawSidebar() {
  const list = clear($("conversations"));
  state.conversations.forEach((conversation) => {
    const active = state.current && state.current.id === conversation.id;
    list.append(
      h(
        "li", { class: active ? "active" : "" },
        h("button", { class: "conv-title", type: "button", onclick: () => select(conversation.id) }, title(conversation)),
        h("button", { class: "icon", type: "button", title: "Rename", onclick: () => rename(conversation) }, "✎"),
        h("button", { class: "icon", type: "button", title: "Delete", onclick: () => remove(conversation) }, "🗑"),
      ),
    );
  });
  $("more-conversations").hidden = state.nextCursor === null;
}

async function loadConversations(more = false) {
  const query = more && state.nextCursor ? `&cursor=${encodeURIComponent(state.nextCursor)}` : "";
  const page = await api(`/conversations?limit=30${query}`);
  state.conversations = more ? [...state.conversations, ...page.items] : page.items;
  state.nextCursor = page.next_cursor;
  drawSidebar();
}

function setCurrent(conversation) {
  state.current = conversation;
  location.hash = conversation ? `c=${conversation.id}` : "";
  $("conv-title").textContent = conversation ? title(conversation) : "New chat";
  $("model").disabled = !conversation;
  if (conversation) $("model").value = conversation.model;
  drawSidebar();
}

async function select(id) {
  let conversation = state.conversations.find((c) => c.id === id);
  try {
    conversation = conversation || (await api(`/conversations/${id}`));
  } catch (error) {
    toast(error.message);
    return setCurrent(null), chat.open(null);
  }
  setCurrent(conversation);
  await Promise.all([chat.open(conversation), datasets.syncAttached()]);
  document.body.classList.remove("nav-open");
}

async function createConversation() {
  try {
    const conversation = await api("/conversations", { method: "POST", body: {} });
    state.conversations.unshift(conversation);
    setCurrent(conversation);
    return conversation;
  } catch (error) {
    toast(error.message);
    return null;
  }
}

async function newChat() {
  setCurrent(null);
  await chat.open(null);
  await datasets.syncAttached();
  document.body.classList.remove("nav-open");
  $("input").focus();
}

async function rename(conversation) {
  const next = prompt("Rename conversation", conversation.title || "");
  if (next === null || !next.trim() || next.trim() === conversation.title) return;
  try {
    const updated = await api(`/conversations/${conversation.id}`, { method: "PATCH", body: { title: next.trim() } });
    replace(updated);
  } catch (error) {
    toast(error.message);
  }
}

async function remove(conversation) {
  if (!confirm(`Delete "${title(conversation)}"? Its messages, results and sandbox are removed. Your datasets stay.`)) return;
  try {
    await api(`/conversations/${conversation.id}`, { method: "DELETE" });
  } catch (error) {
    toast(error.status === 409 ? "Cancel the run in progress before deleting this conversation." : error.message);
    return;
  }
  state.conversations = state.conversations.filter((c) => c.id !== conversation.id);
  if (state.current && state.current.id === conversation.id) {
    setCurrent(null);
    await chat.open(null);
    await datasets.syncAttached();
  } else {
    drawSidebar();
  }
}

function replace(updated) {
  state.conversations = state.conversations.map((c) => (c.id === updated.id ? updated : c));
  if (state.current && state.current.id === updated.id) {
    state.current = updated;
    $("conv-title").textContent = title(updated);
    $("model").value = updated.model;
  }
  drawSidebar();
}

// ---- header ---------------------------------------------------------------------------------

function fillModels(models) {
  state.allowedModels = models;
  const select = clear($("model"));
  models.forEach((m) => select.append(h("option", { value: m }, m)));
  if (state.current) select.value = state.current.model;
}

async function changeModel() {
  const conversation = state.current;
  if (!conversation) return;
  try {
    replace(await api(`/conversations/${conversation.id}`, { method: "PATCH", body: { model: $("model").value } }));
  } catch (error) {
    $("model").value = conversation.model;
    toast(error.status === 409 ? "The model can't change while a run is in progress." : error.message);
  }
}

// ---- boot -----------------------------------------------------------------------------------

const chat = new Chat({
  onUsage: showUsage,
  onActiveChange: (busy) => {
    $("model").disabled = busy || !state.current;
    $("datasets-open").disabled = false;
  },
  onRunEnded: async (conversation, { started }) => {
    refreshUsage();
    if (!conversation) return;
    try {
      // The first run names the conversation; pick that up (and anything else) from the server.
      replace(await api(`/conversations/${conversation.id}`));
    } catch {
      /* cosmetic */
    }
  },
  ensureConversation: createConversation,
});

const datasets = new DatasetsPanel({
  getConversation: () => state.current,
  onAttachedChange: () => {},
});

const settings = new SettingsPanel({
  onSaved: (saved) => fillModels(saved.allowed_models),
});

async function boot() {
  const info = await (await fetch("/session-info")).json();
  if (!info.authenticated) {
    $("signin").hidden = false;
    return;
  }
  $("app").hidden = false;
  $("user-name").textContent = info.name || info.email || "Signed in";
  $("logout").addEventListener("click", () => (window.location.href = "/logout"));
  $("new-chat").addEventListener("click", newChat);
  $("more-conversations").addEventListener("click", () => loadConversations(true).catch((e) => toast(e.message)));
  $("model").addEventListener("change", changeModel);
  $("nav-toggle").addEventListener("click", () => document.body.classList.toggle("nav-open"));
  onNetworkChange((online) => ($("offline").hidden = online));

  try {
    const [, current] = await Promise.all([loadConversations(), settings.load()]);
    fillModels(current.allowed_models);
  } catch (error) {
    toast(error.message);
  }
  refreshUsage();

  const match = /^#c=([0-9a-f-]{36})$/.exec(location.hash);
  if (match) await select(match[1]);
  else await newChat();
}

boot();
