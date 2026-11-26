/* PMC Visual Board: a structured flow editor for Pulover's Macro Creator projects. */
"use strict";

const S = {
  token: "", palette: [], kinds: {}, level: "standard",
  files: [], path: "", mtime: null, macros: [], cur: 0,
  sel: null, pendingSlot: null, dirty: false, undo: [], redo: [], zoom: 1,
  byId: new Map(), nextId: 1, saving: false,
};

const GROUP_COLOR = { "Mouse": "--g-mouse", "Keyboard": "--g-keyboard", "Apps & windows": "--g-apps", "Flow": "--g-flow", "Data": "--g-data", "Advanced": "--g-advanced" };
const ICONS = { cursor: "↖", move: "✥", text: "T", key: "⌘", app: "▣", hourglass: "⧗", window: "❐", close: "✕", clock: "◷",
  branch: "⑂", loop: "↻", stop: "■", call: "↪", var: "x", message: "✉", file: "≡", note: "✎", code: "{}" };

// ---------------------------------------------------------------- utilities
const $ = (id) => document.getElementById(id);
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.setAttribute("style", v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return e;
}
function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = "toast"), err ? 5000 : 2200);
}
async function api(method, path, body) {
  const r = await fetch("/api/" + path, {
    method, headers: { "X-Board-Token": S.token, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw Object.assign(new Error(data.error || r.statusText), { status: r.status });
  return data;
}
const clone = (o) => JSON.parse(JSON.stringify(o));
function strip(tree) {
  return JSON.parse(JSON.stringify(tree, (k, v) => (k === "_id" || k === "view" ? undefined : v)));
}
const macro = () => S.macros[S.cur];

// ------------------------------------------------------------ node helpers
function kindOf(node) {
  if (node.t !== "step") return node.t;
  const v = node.spec || node.view;
  return v ? v.kind : null;
}
function groupOf(node) {
  const k = kindOf(node);
  if (k && S.kinds[k]) return S.kinds[k].group;
  if (node.t === "if" || node.t === "loop") return "Flow";
  const t = (node.step && node.step.type) || "";
  if (/Click|Mouse/.test(t)) return "Mouse";
  if (/Send/.test(t)) return "Keyboard";
  if (/^Win|^Run|^Control|^Process/.test(t)) return "Apps & windows";
  if (/Sleep|Loop|If|Gosub|Goto|Return|Break|Continue|KeyWait/.test(t)) return "Flow";
  if (/Variable|Function|MsgBox|File|Ini|String|Random|Method/.test(t)) return "Data";
  return "Advanced";
}
function leafEnabled(n) { return n.spec ? n.spec.enabled !== false : n.step ? n.step.enabled !== false : true; }
function leafNote(n) { return n.spec ? n.spec.note || "" : (n.step && n.step.comment) || ""; }
function headVal(head, specKey, rawKey) { return head && head.kind ? head[specKey] : head ? head[rawKey] : ""; }

function summary(node) {
  if (node.t === "if") {
    const h = node.branches[0]?.head;
    const kindTitle = h && !h.kind && h.action && h.action !== "Compare Variables" ? h.action : "If";
    return { title: kindTitle, sub: headVal(h, "condition", "details") };
  }
  if (node.t === "loop") {
    const h = node.head;
    if (!h || h.kind || h.type === "Loop") { const c = headVal(h, "count", "repeat"); return { title: "Repeat", sub: !c || c === "0" ? "until Break" : `${c} times` }; }
    return { title: `Loop (${h.type})`, sub: h.details };
  }
  const v = node.spec || node.view;
  if (!v) { const s = node.step || {}; return { title: s.action || s.type || "Step", sub: s.action === s.details ? s.type : s.details }; }
  const label = S.kinds[v.kind]?.label || v.kind;
  const sub = {
    click: () => `${v.button || "Left"} ×${v.count || 1} at ${v.x}, ${v.y}`,
    mouse_move: () => `to ${v.x}, ${v.y}`,
    type_text: () => `"${v.text}"` + (v.window ? ` → ${v.window}` : ""),
    send_keys: () => v.keys,
    run: () => v.target, win_wait: () => v.window, win_activate: () => v.window, win_close: () => v.window,
    sleep: () => `${v.ms} ms`, assign: () => `${v.var} = ${v.value}`, msgbox: () => v.text,
    file_append: () => v.path, comment: () => v.text, command: () => `${v.name} ${v.params || ""}`,
    gosub: () => v.label, break: () => "",
  }[v.kind];
  return { title: label, sub: sub ? sub() : "" };
}

function newNode(kind) {
  const def = S.kinds[kind];
  const params = {};
  for (const f of def?.fields || []) params[f.name] = f.default;
  if (kind === "if") return { t: "if", branches: [{ head: { kind: "if", condition: params.condition }, body: [] }], else: { head: null, body: [] }, end: null };
  if (kind === "loop") return { t: "loop", head: { kind: "loop", count: params.count }, body: [], end: null };
  const spec = { kind, ...params };
  return { t: "step", spec, view: spec };
}

// Walk the tree giving every node and list an id, and record parents.
function index() {
  S.byId.clear();
  const visit = (list, owner) => {
    list._id = list._id || S.nextId++;
    S.byId.set(list._id, { list, owner });
    list.forEach((n) => {
      n._id = n._id || S.nextId++;
      S.byId.set(n._id, { node: n, list });
      if (n.t === "if") { n.branches.forEach((b) => visit(b.body, n)); if (n.else) visit(n.else.body, n); }
      if (n.t === "loop") visit(n.body, n);
    });
  };
  if (macro()) visit(macro().tree, null);
}
function contains(node, list) {
  if (node.t === "if") return node.branches.some((b) => b.body === list || b.body.some((n) => contains(n, list))) || (node.else && (node.else.body === list || node.else.body.some((n) => contains(n, list))));
  if (node.t === "loop") return node.body === list || node.body.some((n) => contains(n, list));
  return false;
}

// ---------------------------------------------------------------- history
function checkpoint() {
  S.undo.push(JSON.stringify(strip(S.macros)));
  if (S.undo.length > 100) S.undo.shift();
  S.redo = [];
  setDirty(true);
}
function restore(from, to) {
  if (!from.length) return;
  to.push(JSON.stringify(strip(S.macros)));
  S.macros = JSON.parse(from.pop());
  S.cur = Math.min(S.cur, S.macros.length - 1);
  S.sel = null; setDirty(true); render();
}
function setDirty(d) { S.dirty = d; $("dirty").hidden = !d; }

// ----------------------------------------------------------------- mutations
function insert(list, at, node) { checkpoint(); list.splice(at, 0, node); S.sel = node._id = S.nextId++; S.pendingSlot = null; render(); }
function addKind(kind) {
  const node = newNode(kind);
  if (S.pendingSlot) { const { list, at } = S.pendingSlot; return insert(list, at, node); }
  const sel = S.sel && S.byId.get(S.sel);
  if (sel && sel.node) return insert(sel.list, sel.list.indexOf(sel.node) + 1, node);
  insert(macro().tree, macro().tree.length, node);
}
function move(id, list, at) {
  const src = S.byId.get(id);
  if (!src || !src.node) return;
  if (src.node === list || contains(src.node, list)) return toast("Can't move a block into itself", true);
  checkpoint();
  const from = src.list.indexOf(src.node);
  src.list.splice(from, 1);
  if (src.list === list && from < at) at--;
  list.splice(at, 0, src.node);
  S.sel = id; render();
}
function removeSel() {
  const s = S.sel && S.byId.get(S.sel);
  if (!s || !s.node) return;
  checkpoint(); s.list.splice(s.list.indexOf(s.node), 1); S.sel = null; render();
}
function duplicateSel() {
  const s = S.sel && S.byId.get(S.sel);
  if (!s || !s.node) return;
  const copy = JSON.parse(JSON.stringify(s.node, (k, v) => (k === "_id" ? undefined : v)));
  insert(s.list, s.list.indexOf(s.node) + 1, copy);
}
function toggleSel() {
  const s = S.sel && S.byId.get(S.sel);
  if (!s || !s.node || s.node.t !== "step") return;
  checkpoint(); const n = s.node;
  if (n.spec) n.spec.enabled = !leafEnabled(n); else n.step.enabled = !leafEnabled(n);
  render();
}

// ----------------------------------------------------------------- render
function render() {
  index();
  renderTabs();
  const c = $("canvas");
  c.replaceChildren();
  c.style.transform = `scale(${S.zoom})`;
  $("zoomLabel").textContent = Math.round(S.zoom * 100) + "%";
  if (!macro()) { c.append(el("div", { class: "muted", text: "Open or create a project to start." })); renderInspector(); return; }
  if (!macro().structured) showBanner("This macro's If/Loop blocks don't balance, so it is shown as a flat list. It still saves unchanged.");
  c.append(el("div", { class: "terminal", text: "Start" }), renderList(macro().tree), el("div", { class: "terminal", text: "End" }));
  renderInspector();
}
function renderTabs() {
  const tabs = $("tabs");
  tabs.replaceChildren(...S.macros.map((m, i) => el("button", {
    class: "tab" + (i === S.cur ? " active" : ""), text: m.name, title: "Double-click to rename",
    onclick: () => { S.cur = i; S.sel = null; render(); },
    ondblclick: () => { const n = prompt("Macro name", m.name); if (n && n !== m.name) { checkpoint(); m.name = n.trim(); render(); } },
  })), el("button", { class: "tab", text: "+", title: "Add macro", onclick: addMacro }));
}
function slot(list, at) {
  const s = el("div", { class: "slot" }, el("button", { title: "Add a step here", text: "+", onclick: (e) => {
    e.stopPropagation(); S.pendingSlot = { list, at };
    document.querySelectorAll(".slot.drop").forEach((x) => x.classList.remove("drop"));
    s.classList.add("drop"); toast("Now click an action on the left");
  } }));
  s.addEventListener("dragover", (e) => { e.preventDefault(); s.classList.add("drop"); });
  s.addEventListener("dragleave", () => s.classList.remove("drop"));
  s.addEventListener("drop", (e) => {
    e.preventDefault(); s.classList.remove("drop");
    const kind = e.dataTransfer.getData("text/pmc-kind"), id = e.dataTransfer.getData("text/pmc-node");
    if (kind) insert(list, at, newNode(kind)); else if (id) move(+id, list, at);
  });
  if (S.pendingSlot && S.pendingSlot.list === list && S.pendingSlot.at === at) s.classList.add("drop");
  return s;
}
function renderList(list) {
  const f = el("div", { class: "flow" });
  f.append(slot(list, 0));
  list.forEach((n, i) => { f.append(renderNode(n), slot(list, i + 1)); });
  return f;
}
function card(node) {
  const { title, sub } = summary(node);
  const g = groupOf(node);
  const def = S.kinds[kindOf(node)];
  const icon = ICONS[def?.icon] || (node.t === "if" ? ICONS.branch : node.t === "loop" ? ICONS.loop : "•");
  const off = node.t === "step" && !leafEnabled(node);
  const c = el("div", {
    class: "node" + (S.sel === node._id ? " sel" : "") + (off ? " off" : ""), draggable: "true", tabindex: "0",
    style: `--gc: var(${GROUP_COLOR[g] || "--g-advanced"})`,
    onclick: (e) => { e.stopPropagation(); S.sel = node._id; S.pendingSlot = null; render(); },
    ondragstart: (e) => { e.stopPropagation(); e.dataTransfer.setData("text/pmc-node", String(node._id)); },
  }, el("div", { class: "ic", text: icon }),
  el("div", {}, el("div", { class: "title", text: title }), sub ? el("div", { class: "sub", text: sub, title: sub }) : null,
    node.t === "step" && leafNote(node) ? el("div", { class: "note", text: leafNote(node) }) : null));
  return c;
}
function renderNode(node) {
  if (node.t === "step") return card(node);
  const b = el("div", { class: "block" }, card(node));
  if (node.t === "loop") {
    b.append(el("div", { class: "loop-body" }, renderList(node.body)));
  } else {
    const cols = node.branches.map((br, i) => el("div", { class: "branch" },
      el("div", { class: "branch-label" }, i === 0 ? "Then" : "Else if", i ? el("span", { class: "cond", text: headVal(br.head, "condition", "details") }) : null),
      renderList(br.body)));
    if (node.else) cols.push(el("div", { class: "branch" }, el("div", { class: "branch-label", text: "Else" }), renderList(node.else.body)));
    b.append(el("div", { class: "branches" }, cols));
  }
  return b;
}
function showBanner(msg, actions = []) {
  const b = $("banner");
  b.replaceChildren(el("span", { text: msg }), ...actions);
  b.hidden = false;
}

// -------------------------------------------------------------- inspector
function field(f, value, onchange) {
  const id = "f_" + f.name;
  if (f.type === "checkbox") return el("label", { class: "check" }, el("input", { type: "checkbox", id, checked: !!value, onchange: (e) => onchange(e.target.checked) }), f.label);
  let input;
  if (f.type === "select") input = el("select", { id, onchange: (e) => onchange(e.target.value) }, (f.options || []).map((o) => el("option", { value: o, selected: o === value, text: o })));
  else if (f.type === "textarea") input = el("textarea", { id, rows: 3, oninput: (e) => onchange(e.target.value) });
  else input = el("input", { id, type: f.type === "number" ? "number" : "text", oninput: (e) => onchange(f.type === "number" ? Number(e.target.value) : e.target.value) });
  if (f.type !== "select") input.value = value ?? "";
  return el("label", { for: id }, f.label, input);
}
let editTimer = null;
function edited() { // coalesce keystrokes into one undo step and redraw the card
  if (!editTimer) checkpoint();
  clearTimeout(editTimer); editTimer = setTimeout(() => (editTimer = null), 800);
  setDirty(true);
  const sel = S.sel; const ins = $("inspector"); const focusId = document.activeElement?.id;
  const keepInspector = ins.contains(document.activeElement);
  index();
  const c = $("canvas"); c.replaceChildren(el("div", { class: "terminal", text: "Start" }), renderList(macro().tree), el("div", { class: "terminal", text: "End" }));
  S.sel = sel;
  if (!keepInspector) renderInspector(); else if (focusId) document.getElementById(focusId)?.focus();
}
function renderInspector() {
  const ins = $("inspector");
  const s = S.sel && S.byId.get(S.sel);
  if (!s || !s.node) {
    ins.replaceChildren(el("div", { class: "empty" }, "Select a step to edit it.", el("br"), el("br"),
      "Drag actions from the left onto a ", el("b", { text: "+" }), " slot, or click an action to add it after the selected step."));
    return;
  }
  const n = s.node, parts = [];
  const { title } = summary(n);
  parts.push(el("h3", { text: title }));
  if (n.t === "step") {
    const v = n.spec || n.view;
    if (v && S.kinds[v.kind]) {
      parts.push(el("div", { class: "kindline", text: S.kinds[v.kind].group }));
      const spec = () => { if (!n.spec) { n.spec = { ...n.view, enabled: leafEnabled(n), note: leafNote(n) }; n.view = n.spec; } return n.spec; };
      for (const f of S.kinds[v.kind].fields) parts.push(field(f, v[f.name], (val) => { spec()[f.name] = val; edited(); }));
      parts.push(field({ name: "note", label: "Note", type: "text" }, leafNote(n), (val) => { spec().note = val; edited(); }));
      parts.push(field({ name: "enabled", label: "Enabled", type: "checkbox" }, leafEnabled(n), (val) => { spec().enabled = val; edited(); }));
      if (n.step && !n.spec) parts.push(rawPreview(n.step));
    } else {
      parts.push(el("div", { class: "kindline", text: `PMC command: ${n.step.type || "?"} (edited as a raw row)` }));
      const raw = [["action", "Action"], ["details", "Details", "textarea"], ["type", "Type"], ["target", "Target / control"], ["window", "Window"], ["repeat", "Repeat"], ["delay", "Delay (ms)"], ["comment", "Note"]];
      for (const [k, label, type] of raw) parts.push(field({ name: k, label, type: type || "text" }, n.step[k], (val) => { n.step[k] = String(val); edited(); }));
      parts.push(field({ name: "enabled", label: "Enabled", type: "checkbox" }, n.step.enabled !== false, (val) => { n.step.enabled = val; edited(); }));
    }
  } else if (n.t === "if") {
    parts.push(el("div", { class: "kindline", text: "Branches run top to bottom; the first true condition wins." }));
    n.branches.forEach((br, i) => {
      const key = br.head && br.head.kind ? "condition" : "details";
      const row = el("div", {}, field({ name: "c" + i, label: i === 0 ? "If" : `Else if #${i}`, type: "text" }, br.head ? br.head[key] : "", (val) => {
        if (!br.head) br.head = { kind: "if", condition: val }; else br.head[key] = val; edited();
      }));
      if (i > 0) row.append(el("button", { class: "ghost danger", text: "Remove else-if", onclick: () => { checkpoint(); n.branches.splice(i, 1); render(); } }));
      parts.push(row);
    });
    parts.push(el("div", { class: "row" },
      el("button", { text: "+ Else if", onclick: () => { checkpoint(); n.branches.push({ head: { action: "[ElseIf] Compare Variables", details: "true", type: "If_Statement", repeat: "1", delay: "0" }, body: [] }); render(); } })),
    field({ name: "hasElse", label: "Has an Else branch", type: "checkbox" }, !!n.else, (val) => {
      checkpoint();
      if (val && !n.else) n.else = { head: null, body: [] };
      else if (!val && n.else) { if (n.else.body.length && !confirm("Delete the Else branch and its steps?")) return render(); n.else = null; }
      render();
    }));
    parts.push(el("p", { class: "muted", text: "Conditions are AutoHotkey expressions, e.g. ErrorLevel = 0, Count > 3, InStr(Title, \"Invoice\")." }));
  } else if (n.t === "loop") {
    const h = n.head;
    if (!h || h.kind || h.type === "Loop") {
      parts.push(field({ name: "count", label: "Times (0 = until Break)", type: "number" }, headVal(h, "count", "repeat"), (val) => {
        if (h.kind) h.count = val; else h.repeat = String(val); edited();
      }));
    } else {
      parts.push(el("div", { class: "kindline", text: `PMC loop type: ${h.type}` }));
      parts.push(field({ name: "details", label: "Loop parameters", type: "text" }, h.details, (val) => { h.details = val; edited(); }));
    }
  }
  parts.push(el("div", { class: "row" },
    n.t === "step" ? el("button", { text: leafEnabled(n) ? "Disable" : "Enable", onclick: toggleSel }) : null,
    el("button", { text: "Duplicate", onclick: duplicateSel }),
    el("button", { class: "danger", text: "Delete", onclick: removeSel })));
  ins.replaceChildren(...parts);
}
function rawPreview(step) {
  const d = el("details", {}, el("summary", { text: "PMC row" }));
  d.append(el("pre", { class: "result", text: ["action", "details", "type", "target", "window", "repeat", "delay"].map((k) => `${k}: ${step[k] ?? ""}`).join("\n") }));
  return d;
}

// --------------------------------------------------------------- palette
function renderPalette() {
  const p = $("palette");
  p.replaceChildren();
  for (const g of S.palette) {
    p.append(el("h4", { text: g.group }));
    for (const k of g.kinds) {
      p.append(el("div", {
        class: "tile", draggable: "true", title: "Drag onto a + slot, or click to add",
        ondragstart: (e) => e.dataTransfer.setData("text/pmc-kind", k.kind),
        onclick: () => addKind(k.kind),
      }, el("span", { class: "ic", style: `background: var(${GROUP_COLOR[g.group]})`, text: ICONS[k.icon] || "•" }), k.label));
    }
  }
}

// ------------------------------------------------------------- file ops
async function loadFiles() {
  const r = await api("GET", "files");
  S.files = r.files;
  const sel = $("fileSelect");
  sel.replaceChildren(...(S.files.length ? [] : [el("option", { value: "", text: "No projects yet" })]),
    ...S.files.map((f) => el("option", { value: f.path, selected: f.path === S.path, text: f.name })));
  return r;
}
async function open(path, keepTab = false) {
  const r = await api("GET", "project?path=" + encodeURIComponent(path));
  const cur = keepTab ? S.cur : 0;
  S.path = r.path; S.mtime = r.mtime; S.macros = r.macros;
  S.cur = Math.min(cur, S.macros.length - 1); S.sel = null; S.undo = []; S.redo = []; S.pendingSlot = null;
  setDirty(false); $("banner").hidden = true;
  $("fileSelect").value = S.path;
  try { history.replaceState(null, "", `#t=${S.token}&f=${encodeURIComponent(S.path)}`); } catch { /* ignore */ }
  render();
}
async function save() {
  if (!S.path) return toast("Create or open a project first", true);
  S.saving = true;
  try {
    const body = { path: S.path, mtime: S.mtime, macros: S.macros.map((m) => ({ index: m.index, name: m.name, hotkey: m.hotkey, repeat: m.repeat, tree: strip(m.tree) })) };
    const r = await api("PUT", "project", body);
    const cur = S.cur;
    S.mtime = r.mtime; S.macros = r.macros; S.cur = Math.min(cur, S.macros.length - 1); S.sel = null;
    setDirty(false); $("banner").hidden = true; render(); toast("Saved");
    await loadFiles();
    return true;
  } catch (e) {
    if (e.status === 409) showBanner(e.message, [el("button", { text: "Reload from disk", onclick: () => open(S.path, true) }), el("button", { class: "danger", text: "Overwrite", onclick: forceSave })]);
    else toast("Save failed: " + e.message, true);
    return false;
  } finally { S.saving = false; }
}
async function forceSave() { S.mtime = null; await save(); }
async function newFile() {
  let name = prompt("New project name", "My automation");
  if (!name) return;
  name = name.trim().replace(/[\\/:*?"<>|]/g, "_");
  if (!name.toLowerCase().endsWith(".pmc")) name += ".pmc";
  try {
    const r = await api("PUT", "project", { path: name, create: true, macros: [{ name: "Macro1", tree: [] }] });
    await loadFiles(); await open(r.path);
  } catch (e) { toast(e.message, true); }
}
function addMacro() {
  const name = prompt("Macro name", `Macro${S.macros.length + 1}`);
  if (!name) return;
  checkpoint();
  S.macros.push({ index: null, name: name.trim(), hotkey: "", repeat: "1", structured: true, tree: [] });
  S.cur = S.macros.length - 1; S.sel = null; render();
}

// --------------------------------------------------------------- running
async function ensureSaved() { return !S.dirty || (await save()); }
function openRun() {
  if (!macro()) return;
  $("runResult").hidden = true;
  $("runDialog").showModal();
}
async function runNow(e) {
  e.preventDefault();
  if (!(await ensureSaved())) return;
  const inputs = {};
  for (const line of $("runInputs").value.split("\n")) {
    const i = line.indexOf("=");
    if (i > 0) inputs[line.slice(0, i).trim()] = line.slice(i + 1);
  }
  const outputs = $("runOutputs").value.split(",").map((s) => s.trim()).filter(Boolean);
  const out = $("runResult");
  out.hidden = false; out.textContent = `Running ${macro().name}…`;
  $("runGo").disabled = true;
  try {
    const r = await api("POST", "run", { path: S.path, macro: S.cur + 1, inputs, outputs, timeout_s: Number($("runTimeout").value) || 120 });
    out.textContent = (r.completed ? "✓ Completed" : r.timed_out ? "⏱ Timed out (stopped)" : "✗ Did not reach the last step") + ` in ${r.seconds}s\n` +
      (Object.keys(r.outputs || {}).length ? "\nOutputs:\n" + Object.entries(r.outputs).map(([k, v]) => `  ${k} = ${v ?? "(not set)"}`).join("\n") : "") +
      (r.note ? `\n\n${r.note}` : "");
  } catch (err) { out.textContent = "Error: " + err.message; }
  finally { $("runGo").disabled = false; }
}
async function play() {
  if (!(await ensureSaved())) return;
  try {
    await api("POST", "pmc/open", { path: S.path });
    await api("POST", "pmc/play", { macro: S.cur + 1 });
    toast("Playing in PMC");
  } catch (e) { toast(e.message, true); }
}
async function stop() { try { await api("POST", "pmc/stop", {}); toast("Stopped"); } catch (e) { toast(e.message, true); } }

// ---------------------------------------------------------------- polling
async function poll() {
  try {
    const st = await api("GET", "pmc/status");
    const p = $("pmcStatus");
    p.className = "pill" + (st.running ? (st.playing || st.recording ? " busy" : " on") : "");
    p.textContent = !st.running ? "PMC: offline" : st.playing ? "PMC: playing" : st.recording ? "PMC: recording" : "PMC: ready";
    $("play").disabled = !st.running; $("stop").disabled = !st.running;
  } catch { /* server gone */ }
  if (S.path && !S.saving) {
    try {
      const { files } = await loadFiles();
      const f = files.find((x) => x.path === S.path);
      if (f && S.mtime !== null && Math.abs(f.mtime - S.mtime) > 1e-3) {
        if (!S.dirty) { await open(S.path, true); toast("Reloaded: the file changed on disk"); }
        else showBanner("This file was changed outside the board (PMC or an AI agent).", [
          el("button", { text: "Reload (lose my edits)", onclick: () => open(S.path, true) }),
          el("button", { class: "danger", text: "Keep mine & overwrite", onclick: forceSave })]);
      }
    } catch { /* ignore */ }
  }
}

// ------------------------------------------------------------------ boot
function zoom(d) { S.zoom = Math.min(1.6, Math.max(0.4, Math.round((S.zoom + d) * 10) / 10)); render(); }
function bindKeys() {
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
    else if (typing) return;
    else if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); restore(S.undo, S.redo); }
    else if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); restore(S.redo, S.undo); }
    else if (mod && e.key.toLowerCase() === "d") { e.preventDefault(); duplicateSel(); }
    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSel(); }
    else if (e.key === "Escape") { S.sel = null; S.pendingSlot = null; render(); }
  });
  $("canvasWrap").addEventListener("click", () => { if (S.sel || S.pendingSlot) { S.sel = null; S.pendingSlot = null; render(); } });
  $("canvasWrap").addEventListener("wheel", (e) => { if (e.ctrlKey) { e.preventDefault(); zoom(e.deltaY < 0 ? 0.1 : -0.1); } }, { passive: false });
  window.addEventListener("beforeunload", (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });
}
async function boot() {
  const h = new URLSearchParams(location.hash.slice(1));
  S.token = h.get("t") || "";
  if (!S.token) { document.body.replaceChildren(el("p", { style: "padding:20px", text: "Open the board with the link printed by pmc-board (it contains an access token)." })); return; }
  $("save").onclick = save; $("run").onclick = openRun; $("runGo").onclick = runNow; $("play").onclick = play; $("stop").onclick = stop;
  $("newFile").onclick = newFile; $("undo").onclick = () => restore(S.undo, S.redo); $("redo").onclick = () => restore(S.redo, S.undo);
  $("zoomIn").onclick = () => zoom(0.1); $("zoomOut").onclick = () => zoom(-0.1);
  $("fileSelect").onchange = (e) => { if (S.dirty && !confirm("Discard unsaved changes?")) { e.target.value = S.path; return; } if (e.target.value) open(e.target.value); };
  bindKeys();
  try {
    const p = await api("GET", "palette");
    S.palette = p.palette; S.level = p.level;
    for (const g of p.palette) for (const k of g.kinds) S.kinds[k.kind] = { ...k, group: g.group };
    renderPalette();
    await loadFiles();
    const want = h.get("f") || (S.files[0] && S.files[0].path);
    if (want) await open(want); else render();
    if (S.level === "readonly") { $("save").disabled = $("run").disabled = $("play").disabled = true; toast("Read-only mode (PMC_MCP_LEVEL=readonly)"); }
  } catch (e) { toast(e.message, true); }
  setInterval(poll, 3000); poll();
}
boot();
