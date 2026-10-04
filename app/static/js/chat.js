// The chat pane: history, sending, the live run (SSE), approvals, artifacts.
import { api, ApiError } from "./api.js";
import { clear, h, toast } from "./dom.js";
import { renderMarkdown } from "./md.js";

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const EVENT_TYPES = [
  "run_started", "run_resumed", "status", "token", "tool_call", "tool_result",
  "artifact", "approval_required", "usage", "done",
];

export class Chat {
  constructor({ onUsage, onRunEnded, onActiveChange, ensureConversation }) {
    this.el = {
      messages: document.getElementById("messages"),
      older: document.getElementById("load-older"),
      input: document.getElementById("input"),
      send: document.getElementById("send"),
      cancel: document.getElementById("cancel"),
      status: document.getElementById("run-status"),
      conn: document.getElementById("conn"),
    };
    this.hooks = { onUsage, onRunEnded, onActiveChange, ensureConversation };
    this.conv = null;
    this.gen = 0; // bumped on every open(): late responses for a previous conversation are dropped
    this.stream = null;
    this.runId = null;
    this.live = null;
    this.lastId = 0;
    this.olderBefore = null;

    this.el.send.addEventListener("click", () => this.send());
    this.el.cancel.addEventListener("click", () => this.cancel());
    this.el.older.addEventListener("click", () => this.loadOlder());
    this.el.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.send();
      }
    });
    this.el.input.addEventListener("input", () => this.autosize());
  }

  get busy() {
    return this.runId !== null;
  }

  autosize() {
    const input = this.el.input;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
  }

  // ---- opening a conversation -------------------------------------------------------------

  async open(conversation) {
    const gen = ++this.gen;
    this.closeStream();
    this.conv = conversation;
    this.runId = null;
    this.live = null;
    this.lastId = 0;
    this.olderBefore = null;
    clear(this.el.messages);
    this.el.older.hidden = true;
    this.setStatus("");
    this.render();
    if (!conversation) return;

    try {
      const [page, active] = await Promise.all([
        api(`/conversations/${conversation.id}/messages?limit=50`),
        api(`/conversations/${conversation.id}/active-run`),
      ]);
      if (gen !== this.gen) return;
      page.items.forEach((m) => this.appendMessage(m));
      this.olderBefore = page.next_before;
      this.el.older.hidden = this.olderBefore === null;
      this.scrollToEnd(true);
      if (active) this.attach(active.id, active.status);
    } catch (error) {
      if (gen === this.gen) toast(error.message);
    }
  }

  async loadOlder() {
    if (this.olderBefore === null || !this.conv) return;
    const gen = this.gen;
    try {
      const page = await api(`/conversations/${this.conv.id}/messages?limit=50&before=${this.olderBefore}`);
      if (gen !== this.gen) return;
      const box = this.el.messages;
      const fromBottom = box.scrollHeight - box.scrollTop;
      box.prepend(...page.items.map((m) => this.messageEl(m)));
      this.olderBefore = page.next_before;
      this.el.older.hidden = this.olderBefore === null;
      box.scrollTop = box.scrollHeight - fromBottom;
    } catch (error) {
      toast(error.message);
    }
  }

  render() {
    const busy = this.busy;
    this.el.input.disabled = busy;
    this.el.send.hidden = busy;
    this.el.cancel.hidden = !busy;
    this.el.send.disabled = false;
    this.el.input.placeholder = busy ? "A run is in progress…" : "Ask about your data";
    this.hooks.onActiveChange(busy);
  }

  // ---- history ---------------------------------------------------------------------------

  messageEl(message) {
    const content = message.content || {};
    if (message.role === "user") {
      return h("div", { class: "msg user", dataset: { id: message.id } }, h("div", { class: "bubble" }, content.text || ""));
    }
    const bubble = h("div", { class: "bubble md" });
    if (content.text) bubble.innerHTML = renderMarkdown(content.text);
    const extras = h("div", { class: "extras" });
    (content.tools || []).forEach((tool) =>
      extras.append(h("span", { class: `chip ${tool.ok ? "" : "bad"}` }, `${tool.ok ? "✓" : "✗"} ${tool.name}`)),
    );
    (content.artifacts || []).forEach((artifact) => extras.append(this.artifactEl(artifact)));
    return h("div", { class: "msg assistant", dataset: { id: message.id } }, bubble, extras.childElementCount ? extras : null);
  }

  appendMessage(message) {
    this.lastId = Math.max(this.lastId, message.id);
    this.el.messages.append(this.messageEl(message));
  }

  async syncHistory() {
    if (!this.conv) return;
    const gen = this.gen;
    const page = await api(`/conversations/${this.conv.id}/messages?limit=20`);
    if (gen !== this.gen) return;
    this.el.messages.querySelectorAll("[data-optimistic]").forEach((el) => el.remove());
    page.items.filter((m) => m.id > this.lastId).forEach((m) => this.appendMessage(m));
  }

  // ---- artifacts -------------------------------------------------------------------------

  artifactEl(artifact) {
    const wrap = h("div", { class: "artifact" });
    const link = h("button", { class: "linkish", type: "button" }, `⬇ ${artifact.filename}`);
    link.addEventListener("click", async () => {
      try {
        const signed = await api(`/artifacts/${artifact.id}`);
        window.location.assign(signed.url); // served as an attachment: downloads, doesn't navigate
      } catch (error) {
        toast(error.message);
      }
    });
    if (artifact.kind === "image") {
      const img = h("img", { alt: artifact.filename, loading: "lazy" });
      img.hidden = true;
      api(`/artifacts/${artifact.id}`)
        .then((signed) => {
          img.src = signed.url;
          img.hidden = false;
        })
        .catch(() => {});
      wrap.append(img);
    }
    wrap.append(link);
    return wrap;
  }

  // ---- sending ---------------------------------------------------------------------------

  async send() {
    const text = this.el.input.value.trim();
    if (!text || this.busy) return;
    this.el.send.disabled = true;
    try {
      if (!this.conv) {
        const created = await this.hooks.ensureConversation();
        if (!created) return;
        this.conv = created;
      }
      const run = await api(`/conversations/${this.conv.id}/runs`, { method: "POST", body: { text } });
      this.el.input.value = "";
      this.autosize();
      this.el.messages.append(
        h("div", { class: "msg user", dataset: { optimistic: "1" } }, h("div", { class: "bubble" }, text)),
      );
      this.attach(run.id, "queued");
      this.scrollToEnd(true);
      this.hooks.onRunEnded(this.conv, { started: true });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        toast("A run is already in progress in this conversation.");
        this.open(this.conv); // pick up the run that is actually active
      } else {
        toast(error.message);
      }
    } finally {
      this.el.send.disabled = false;
    }
  }

  async cancel() {
    if (!this.runId) return;
    this.el.cancel.disabled = true;
    this.setStatus("Cancelling…");
    try {
      await api(`/runs/${this.runId}/cancel`, { method: "POST" });
    } catch (error) {
      toast(error.message);
      this.el.cancel.disabled = false;
    }
  }

  // ---- the live run ----------------------------------------------------------------------

  attach(runId, status) {
    this.closeStream();
    this.runId = runId;
    this.render();
    this.el.cancel.disabled = false;
    this.live = { root: h("div", { class: "msg assistant live-msg" }), text: null, blocks: new Map(), approval: null, gen: this.gen, runId, errored: false };
    this.el.messages.append(this.live.root);
    this.setStatus(status === "awaiting_approval" ? "Waiting for your approval…" : "Starting…");

    // ?from=0 replays the run from its start, which is what a reload needs.
    const source = new EventSource(`/api/v1/runs/${runId}/events?from=0`);
    this.stream = source;
    source.addEventListener("open", () => this.setConn(false));
    EVENT_TYPES.forEach((type) =>
      source.addEventListener(type, (event) => this.handle(runId, type, JSON.parse(event.data))),
    );
    // "error" is both the native connection event and the contract's run error: only the latter has data.
    source.addEventListener("error", (event) => {
      if (event.data !== undefined) {
        this.handle(runId, "error", JSON.parse(event.data));
      } else if (source.readyState === EventSource.CONNECTING) {
        this.setConn(true);
      } else if (source.readyState === EventSource.CLOSED) {
        this.streamLost(runId);
      }
    });
  }

  async streamLost(runId) {
    // The server refused the stream (e.g. it expired): the run's state and the saved messages are the truth.
    try {
      const run = await api(`/runs/${runId}`);
      if (TERMINAL.has(run.status)) return this.finish(runId, run.status);
    } catch (error) {
      toast(error.message);
    }
    this.setConn(false);
    this.setStatus("The live stream was lost. Reload the page to reattach.");
  }

  handle(runId, type, data) {
    const live = this.live;
    if (!live || live.runId !== runId || live.gen !== this.gen) return;
    switch (type) {
      case "run_resumed":
        // Redelivery/approval resume: the in-progress text is replayed or rewritten, so start it again.
        if (live.text) {
          live.text.el.replaceChildren();
          live.text.raw = "";
        }
        this.dropApproval();
        this.setStatus("Resumed…");
        break;
      case "status":
        this.setStatus(data.message);
        break;
      case "token":
        this.setStatus("");
        this.appendToken(data.text);
        break;
      case "tool_call":
        this.setStatus("");
        this.addToolCall(data);
        break;
      case "tool_result":
        this.addToolResult(data);
        break;
      case "artifact":
        live.root.append(this.artifactEl(data));
        break;
      case "approval_required":
        this.showApproval(data);
        break;
      case "usage":
        this.hooks.onUsage({ used: data.day_used, limit: data.day_limit });
        break;
      case "error":
        live.errored = true;
        live.root.append(h("div", { class: "notice bad" }, `Run failed: ${data.message}`));
        break;
      case "done":
        this.finish(runId, data.status);
        break;
    }
    this.scrollToEnd(false);
  }

  appendToken(text) {
    const live = this.live;
    if (!live.text) {
      const el = h("div", { class: "bubble md" });
      live.root.append(el);
      live.text = { el, raw: "", pending: false };
    }
    const segment = live.text;
    segment.raw += text;
    if (!segment.pending) {
      segment.pending = true;
      requestAnimationFrame(() => {
        segment.pending = false;
        segment.el.innerHTML = renderMarkdown(segment.raw);
      });
    }
  }

  addToolCall({ tool_call_id: id, name, args }) {
    const live = this.live;
    live.text = null; // the next tokens start a new text segment, after this tool
    const code = args && typeof args.code === "string" ? args.code : JSON.stringify(args ?? {}, null, 2);
    const output = h("pre", { class: "tool-output" }, "Running…");
    const block = h(
      "details",
      { class: "tool", open: name === "run_python" },
      h("summary", {}, `🔧 ${name}`),
      h("pre", { class: "tool-code" }, code),
      output,
    );
    live.blocks.set(id, { block, output });
    live.root.append(block);
  }

  addToolResult({ tool_call_id: id, output, error }) {
    const entry = this.live.blocks.get(id);
    if (entry) {
      entry.output.textContent = output || "(no output)";
      entry.block.classList.toggle("bad", Boolean(error));
      entry.block.querySelector("summary").textContent += error ? " — failed" : " — done";
    }
    if (this.live.approval && this.live.approval.id === id) this.dropApproval();
  }

  // ---- approvals -------------------------------------------------------------------------

  showApproval({ tool_call_id: id, code }) {
    const live = this.live;
    this.dropApproval();
    const editor = h("textarea", { class: "approval-code", rows: Math.min(14, code.split("\n").length + 1), spellcheck: "false" });
    editor.value = code;
    const reason = h("input", { type: "text", placeholder: "Reason for rejecting (optional)", maxlength: "1000" });
    const setDisabled = (disabled) => card.querySelectorAll("button").forEach((b) => (b.disabled = disabled));
    const decide = async (decision) => {
      const body = { decision };
      if (decision === "edit") body.edited_code = editor.value;
      if (decision === "reject" && reason.value.trim()) body.message = reason.value.trim();
      setDisabled(true);
      try {
        await api(`/runs/${live.runId}/approve`, { method: "POST", body });
        this.dropApproval();
        this.setStatus("Resuming…");
      } catch (error) {
        toast(error.message);
        setDisabled(false);
        if (error.status === 409) this.open(this.conv);
      }
    };
    const card = h(
      "div", { class: "approval" },
      h("strong", {}, "The agent wants to run this code"),
      editor,
      h(
        "div", { class: "row" },
        h("button", { class: "primary", type: "button", onclick: () => decide(editor.value === code ? "approve" : "edit") }, "Approve"),
        h("button", { type: "button", onclick: () => decide("reject") }, "Reject"),
        reason,
      ),
      h("small", {}, "If you edit the code, your edited version is what runs."),
    );
    live.approval = { id, card };
    live.root.append(card);
    this.setStatus("Waiting for your approval…");
  }

  dropApproval() {
    if (this.live?.approval) {
      this.live.approval.card.remove();
      this.live.approval = null;
    }
  }

  // ---- finishing -------------------------------------------------------------------------

  async finish(runId, status) {
    if (this.runId !== runId) return;
    const live = this.live;
    this.closeStream();
    this.runId = null;
    this.setStatus("");
    try {
      await this.syncHistory();
    } catch (error) {
      toast(error.message);
    }
    if (live) {
      live.root.remove();
      if (status === "cancelled") {
        this.el.messages.append(h("div", { class: "notice" }, "Run cancelled."));
      } else if (status === "failed" && !live.errored) {
        this.el.messages.append(h("div", { class: "notice bad" }, "The run failed."));
      } else if (live.errored) {
        // The saved message (if any) is shown; keep the reason visible too.
        const reason = live.root.querySelector(".notice.bad");
        if (reason) this.el.messages.append(reason);
      }
    }
    this.live = null;
    this.render();
    this.scrollToEnd(false);
    this.hooks.onRunEnded(this.conv, { started: false });
    this.el.input.focus();
  }

  closeStream() {
    if (this.stream) this.stream.close();
    this.stream = null;
    this.setConn(false);
  }

  // ---- small UI bits ---------------------------------------------------------------------

  setStatus(text) {
    this.el.status.textContent = text;
    this.el.status.hidden = !text;
  }

  setConn(reconnecting) {
    this.el.conn.hidden = !reconnecting;
  }

  scrollToEnd(force) {
    const box = this.el.messages;
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 140;
    if (force || near) box.scrollTop = box.scrollHeight;
  }
}
