// Dialogs: datasets (upload / attach), and settings with the memories viewer.
import { api } from "./api.js";
import { clear, formatBytes, formatNumber, h, toast } from "./dom.js";

const POLL_MS = 1500;
const POLL_LIMIT = 80; // ~2 minutes for the worker to process an upload

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---- datasets -------------------------------------------------------------------------------

export class DatasetsPanel {
  constructor({ getConversation, onAttachedChange }) {
    this.getConversation = getConversation;
    this.onAttachedChange = onAttachedChange;
    this.dialog = document.getElementById("datasets-dialog");
    this.list = document.getElementById("datasets-list");
    this.chips = document.getElementById("attached");
    this.file = document.getElementById("file-input");
    this.datasets = [];
    this.attached = new Set();
    this.uploads = new Map(); // temp id -> {name, percent, state}

    document.getElementById("datasets-open").addEventListener("click", () => this.open());
    document.getElementById("datasets-close").addEventListener("click", () => this.dialog.close());
    document.getElementById("upload-btn").addEventListener("click", () => this.file.click());
    this.file.addEventListener("change", () => {
      [...this.file.files].forEach((f) => this.upload(f));
      this.file.value = "";
    });
  }

  async open() {
    this.dialog.showModal();
    await this.refresh();
  }

  async refresh() {
    try {
      const conversation = this.getConversation();
      const [datasets, attached] = await Promise.all([
        api("/datasets"),
        conversation ? api(`/conversations/${conversation.id}/datasets`) : [],
      ]);
      this.datasets = datasets;
      this.attached = new Set(attached.map((d) => d.id));
      this.draw();
    } catch (error) {
      toast(error.message);
    }
  }

  // Called when the open conversation changes.
  async syncAttached() {
    const conversation = this.getConversation();
    this.attached = new Set();
    if (conversation) {
      try {
        const attached = await api(`/conversations/${conversation.id}/datasets`);
        if (conversation === this.getConversation()) this.attached = new Set(attached.map((d) => d.id));
        this.datasets = this.datasets.length ? this.datasets : await api("/datasets");
      } catch (error) {
        toast(error.message);
      }
    }
    this.draw();
  }

  draw() {
    const conversation = this.getConversation();
    clear(this.chips);
    this.datasets
      .filter((d) => this.attached.has(d.id))
      .forEach((d) => this.chips.append(h("span", { class: "chip" }, `📎 ${d.filename}`)));

    clear(this.list);
    for (const [id, upload] of this.uploads) {
      this.list.append(
        h("li", {}, h("span", { class: "grow" }, upload.name),
          upload.state === "uploading" ? h("progress", { max: "100", value: String(upload.percent) }) : h("span", { class: "badge" }, upload.state)),
      );
    }
    if (!this.datasets.length && !this.uploads.size) this.list.append(h("li", { class: "muted" }, "No datasets yet. Upload a .csv, .xlsx or .xls file."));
    for (const d of this.datasets) {
      const attached = this.attached.has(d.id);
      const sheets = (d.sheets || []).map((s) => `${s.name} (${formatNumber(s.row_count)} rows)`).join(", ");
      this.list.append(
        h(
          "li", {},
          h("div", { class: "grow" },
            h("div", {}, d.filename, " ", h("span", { class: `badge ${d.status}` }, d.status)),
            h("small", { class: "muted" }, [formatBytes(d.size_bytes), sheets, d.reject_reason].filter(Boolean).join(" · ")),
          ),
          d.status === "ready" && conversation
            ? h("button", { type: "button", onclick: () => this.toggle(d, attached) }, attached ? "Detach" : "Attach")
            : null,
          h("button", { type: "button", class: "danger", onclick: () => this.remove(d) }, "Delete"),
        ),
      );
    }
  }

  async toggle(dataset, attached) {
    const conversation = this.getConversation();
    try {
      if (attached) await api(`/conversations/${conversation.id}/datasets/${dataset.id}`, { method: "DELETE" });
      else await api(`/conversations/${conversation.id}/datasets`, { method: "POST", body: { dataset_id: dataset.id } });
      attached ? this.attached.delete(dataset.id) : this.attached.add(dataset.id);
      this.draw();
      this.onAttachedChange();
    } catch (error) {
      toast(error.message);
    }
  }

  async remove(dataset) {
    if (!confirm(`Delete ${dataset.filename}? It is removed from all conversations.`)) return;
    try {
      await api(`/datasets/${dataset.id}`, { method: "DELETE" });
      this.datasets = this.datasets.filter((d) => d.id !== dataset.id);
      this.attached.delete(dataset.id);
      this.draw();
    } catch (error) {
      toast(error.message);
    }
  }

  // request signed URL -> PUT to GCS with progress -> complete -> wait for processing -> attach
  async upload(file) {
    const key = crypto.randomUUID();
    const state = { name: file.name, percent: 0, state: "uploading" };
    this.uploads.set(key, state);
    this.draw();
    const conversation = this.getConversation();
    try {
      const created = await api("/datasets", { method: "POST", body: { filename: file.name, size_bytes: file.size } });
      await this.put(created.upload, file, (percent) => {
        state.percent = percent;
        this.draw();
      });
      state.state = "processing";
      this.draw();
      let dataset = await api(`/datasets/${created.dataset.id}/complete`, { method: "POST" });
      for (let i = 0; i < POLL_LIMIT && dataset.status === "pending"; i++) {
        await sleep(POLL_MS);
        dataset = await api(`/datasets/${dataset.id}`);
      }
      if (dataset.status === "ready") {
        if (conversation && conversation === this.getConversation()) {
          await api(`/conversations/${conversation.id}/datasets`, { method: "POST", body: { dataset_id: dataset.id } });
        }
        toast(`${file.name} is ready`, "ok");
      } else if (dataset.status === "rejected") {
        toast(`${file.name} was rejected: ${dataset.reject_reason || "unsupported file"}`);
      } else {
        toast(`${file.name} is still processing. Check back shortly.`);
      }
    } catch (error) {
      toast(`Upload of ${file.name} failed: ${error.message}`);
    } finally {
      this.uploads.delete(key);
      await this.refresh();
      this.onAttachedChange();
    }
  }

  put(upload, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(upload.method, upload.url);
      Object.entries(upload.headers).forEach(([name, value]) => xhr.setRequestHeader(name, value));
      xhr.upload.onprogress = (event) => event.lengthComputable && onProgress(Math.round((event.loaded / event.total) * 100));
      xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`storage rejected the file (${xhr.status})`)));
      xhr.onerror = () => reject(new Error("network error while uploading"));
      xhr.send(file);
    });
  }
}

// ---- settings + memories ------------------------------------------------------------------

export class SettingsPanel {
  constructor({ onSaved }) {
    this.onSaved = onSaved;
    this.dialog = document.getElementById("settings-dialog");
    this.approval = document.getElementById("setting-approval");
    this.model = document.getElementById("setting-model");
    this.memories = document.getElementById("memories-list");
    this.allowed = [];

    document.getElementById("settings-open").addEventListener("click", () => this.open());
    document.getElementById("settings-close").addEventListener("click", () => this.dialog.close());
    document.getElementById("settings-save").addEventListener("click", () => this.save());
  }

  async load() {
    const settings = await api("/settings");
    this.allowed = settings.allowed_models;
    return settings;
  }

  async open() {
    this.dialog.showModal();
    try {
      const settings = await this.load();
      this.approval.checked = settings.require_code_approval;
      clear(this.model).append(h("option", { value: "" }, "Server default"));
      this.allowed.forEach((m) => this.model.append(h("option", { value: m }, m)));
      this.model.value = settings.default_model || "";
      await this.loadMemories();
    } catch (error) {
      toast(error.message);
    }
  }

  async save() {
    try {
      // PUT needs the whole body: send both fields every time.
      const saved = await api("/settings", {
        method: "PUT",
        body: { require_code_approval: this.approval.checked, default_model: this.model.value || null },
      });
      this.onSaved(saved);
      this.dialog.close();
      toast("Settings saved", "ok");
    } catch (error) {
      toast(error.message);
    }
  }

  async loadMemories() {
    const items = await api("/memories");
    clear(this.memories);
    if (!items.length) this.memories.append(h("li", { class: "muted" }, "Nothing remembered yet."));
    items.forEach((m) =>
      this.memories.append(
        h("li", {}, h("span", { class: "grow" }, m.text),
          h("button", { type: "button", class: "danger", onclick: () => this.forget(m) }, "Forget")),
      ),
    );
  }

  async forget(memory) {
    try {
      await api(`/memories/${memory.id}`, { method: "DELETE" });
      await this.loadMemories();
    } catch (error) {
      toast(error.message);
    }
  }
}
