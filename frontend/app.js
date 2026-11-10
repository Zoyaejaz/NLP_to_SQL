/* NLP2SQL Pro – vanilla JS frontend (no build step). */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, props = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  kids.flat().forEach((k) => k != null && el.append(k.nodeType ? k : document.createTextNode(k)));
  return el;
};

const state = { conversation: [], busy: false, charts: new Set() };
const STEPS = [
  ["intent", "Understand"],
  ["generate", "Write SQL"],
  ["validate", "Validate"],
  ["execute", "Execute"],
  ["summarize", "Explain"],
];

/* ───────────────────────── API ───────────────────────── */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const d = (await r.json()).detail; msg = (typeof d === "string" ? d : d?.message) || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

/* ───────────────────────── Sidebar ───────────────────────── */
async function loadHealth() {
  try {
    const d = await api("/api/health");
    $("#pill-db").className = "pill " + (d.database ? "ok" : "bad");
    $("#pill-llm").className = "pill " + (d.llm_configured ? "ok" : "bad");
    $("#pill-llm").lastChild.textContent = d.llm_configured ? d.model : "No API key";
    $("#pill-llm").title = d.llm_configured ? "Groq model" : "Set GROQ_API_KEY in .env";
  } catch {
    $("#pill-db").className = "pill bad";
    $("#pill-llm").className = "pill bad";
  }
}

async function loadSchema(refresh = false) {
  const list = $("#schema-list");
  try {
    const d = await api(refresh ? "/api/schema/refresh" : "/api/schema", refresh ? { method: "POST" } : undefined);
    $("#schema-meta").textContent = `${d.tables.length} tables · #${d.fingerprint}`;
    list.replaceChildren(
      ...d.tables.map((t) =>
        h("details", {},
          h("summary", {},
            h("span", { text: t.name }),
            t.uploaded ? h("span", { class: "tag up", text: "UPLOADED" }) : null,
            h("span", { class: "rows", text: t.row_estimate ? `~${t.row_estimate}` : "" }),
            t.uploaded ? h("button", { class: "icon-btn del", title: "Remove this uploaded table", "aria-label": "Remove " + t.name,
              onclick: (e) => { e.preventDefault(); e.stopPropagation(); removeTable(t.name); } }, "✕") : null),
          h("div", { class: "cols" },
            t.columns.map((c) =>
              h("div", { class: "col", title: c.references ? `→ ${c.references}` : c.sample_values?.join(", ") || "" },
                h("span", { class: "cname", text: c.name }),
                c.primary_key ? h("span", { class: "tag pk", text: "PK" }) : null,
                c.references ? h("span", { class: "tag fk", text: "FK" }) : null,
                h("span", { class: "ctype", text: c.type.toLowerCase() })))))));
  } catch (e) {
    list.replaceChildren(h("div", { class: "muted small", text: "Could not load schema: " + e.message }));
  }
}

async function loadHistory() {
  const list = $("#history-list");
  try {
    const items = await api("/api/history?limit=40");
    if (!items.length) { list.replaceChildren(h("div", { class: "muted small", text: "Nothing yet." })); return; }
    list.replaceChildren(...items.map((i) =>
      h("button", { class: "h-item", onclick: () => ask(i.question) },
        h("div", { class: "q" }, h("span", { class: `dot-s ${i.ok ? "" : "fail"}` }), h("span", { text: i.question })),
        h("div", { class: "meta", text: i.ok
          ? `${i.row_count ?? 0} rows · ${i.latency_ms} ms · ${i.llm_calls} LLM${i.cached ? " · cached" : ""}`
          : "failed" }))));
  } catch (e) { list.replaceChildren(h("div", { class: "muted small", text: e.message })); }
}

async function loadStats() {
  try {
    const s = await api("/api/stats");
    const rate = s.total ? Math.round((s.succeeded / s.total) * 100) + "%" : "–";
    const cells = [
      [s.total, "queries"], [rate, "success rate"], [s.avg_latency_ms + " ms", "avg latency"],
      [s.avg_llm_calls, "avg LLM calls"], [s.needed_retry, "self-corrected"], [s.cache_hits, "cache hits"],
    ];
    $("#stats-grid").replaceChildren(...cells.map(([v, l]) => h("div", { class: "stat" }, h("b", { text: String(v) }), h("span", { text: l }))));
  } catch {}
}

$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + t.dataset.tab));
  if (t.dataset.tab === "history") loadHistory();
  if (t.dataset.tab === "stats") loadStats();
}));
$("#btn-refresh-schema").onclick = () => loadSchema(true);
$("#btn-clear-history").onclick = async () => { await api("/api/history", { method: "DELETE" }); loadHistory(); loadStats(); };
$("#btn-menu").onclick = () => $("#sidebar").classList.toggle("open");

/* ───────────────────────── Theme ───────────────────────── */
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch {}
}
try { const t = localStorage.getItem("theme"); if (t) setTheme(t); } catch {}
$("#btn-theme").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

/* ───────────────────────── SQL highlighting ───────────────────────── */
const KW = new Set(("select from where group by order having limit offset join left right inner outer full cross on as and or not in is null like ilike between case when then else end " +
  "distinct union all intersect except with over partition asc desc exists any true false interval cast filter using natural").split(" "));
function highlightSQL(sql) {
  const re = /(--[^\n]*)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?|([\s\S])/g;
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  let out = "", m;
  while ((m = re.exec(sql))) {
    if (m[1]) out += `<span class="c">${esc(m[1])}</span>`;
    else if (m[2]) out += `<span class="s">${esc(m[2])}</span>`;
    else if (m[3]) out += `<span class="n">${m[3]}</span>`;
    else if (m[4]) {
      const w = m[4];
      if (KW.has(w.toLowerCase())) out += `<span class="k">${esc(w)}</span>${esc(m[5] || "")}`;
      else if (m[5]) out += `<span class="f">${esc(w)}</span>${esc(m[5])}`;
      else out += esc(w);
    } else out += esc(m[0]);
  }
  return out;
}

/* ───────────────────────── Result rendering ───────────────────────── */
const isNum = (v) => typeof v === "number";
const fmt = (v) => (isNum(v) ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(v));

function buildTable(columns, rows) {
  if (!rows.length) return h("div", { class: "empty-res", text: "No rows returned." });
  const numeric = columns.map((_, i) => rows.every((r) => r[i] == null || isNum(r[i])));
  let sortCol = -1, asc = true;
  const tbody = h("tbody");
  const draw = (data) => tbody.replaceChildren(...data.map((r) => h("tr", {}, r.map((v, i) =>
    h("td", { class: numeric[i] ? "num" : "" }, v == null ? h("span", { class: "null", text: "NULL" }) : fmt(v))))));
  draw(rows);
  const ths = columns.map((c, i) => h("th", { title: "Click to sort", onclick: () => {
    asc = sortCol === i ? !asc : true; sortCol = i;
    const sorted = [...rows].sort((a, b) => {
      const x = a[i], y = b[i];
      if (x == null) return 1; if (y == null) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1);
    });
    draw(sorted);
  } }, c));
  return h("div", { class: "table-wrap" }, h("table", { class: "res" }, h("thead", {}, h("tr", {}, ths)), tbody));
}

function chartable(columns, rows) {
  if (rows.length < 2 || rows.length > 60 || columns.length < 2) return null;
  const numCols = columns.map((_, i) => i).filter((i) => rows.every((r) => r[i] == null || isNum(r[i])));
  const labelCol = columns.findIndex((_, i) => !numCols.includes(i));
  if (labelCol === -1 && numCols.length < 2) return null;
  const lc = labelCol === -1 ? 0 : labelCol;
  const vcs = numCols.filter((i) => i !== lc).slice(0, 3);
  if (!vcs.length) return null;
  const dateLike = rows.every((r) => /^\d{4}-\d{2}/.test(String(r[lc])));
  return { lc, vcs, type: dateLike ? "line" : "bar" };
}

function buildChart(columns, rows, spec) {
  const css = getComputedStyle(document.documentElement);
  const palette = ["--accent", "--teal", "--blue"].map((v) => css.getPropertyValue(v).trim());
  const muted = css.getPropertyValue("--muted").trim(), line = css.getPropertyValue("--line").trim();
  const box = h("div", { class: "chart-box" });
  const canvas = h("canvas");
  box.append(canvas);
  setTimeout(() => {
    if (!window.Chart) { box.textContent = "Chart.js failed to load."; return; }
    const chart = new Chart(canvas, {
      type: spec.type,
      data: {
        labels: rows.map((r) => String(r[spec.lc])),
        datasets: spec.vcs.map((i, k) => ({
          label: columns[i], data: rows.map((r) => r[i]),
          backgroundColor: palette[k] + "cc", borderColor: palette[k], borderWidth: 2, borderRadius: 6, tension: 0.3,
        })),
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: spec.vcs.length > 1, labels: { color: muted } } },
        scales: {
          x: { ticks: { color: muted, maxRotation: 50 }, grid: { color: line } },
          y: { ticks: { color: muted }, grid: { color: line } },
        },
      },
    });
    state.charts.add(chart);
  }, 0);
  return box;
}

function copyBtn(getText) {
  const b = h("button", { class: "btn sm", text: "Copy" });
  b.onclick = async () => {
    try { await navigator.clipboard.writeText(getText()); b.textContent = "Copied ✓"; } catch { b.textContent = "Press Ctrl+C"; }
    setTimeout(() => (b.textContent = "Copy"), 1400);
  };
  return b;
}

function downloadCSV(columns, rows) {
  const q = (v) => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const csv = [columns.map(q).join(","), ...rows.map((r) => r.map(q).join(","))].join("\n");
  const a = h("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })), download: "result.csv" });
  a.click();
  URL.revokeObjectURL(a.href);
}

/* Results card (table + optional chart). Re-used after "Edit & run". */
function buildResultsCard(d) {
  const body = h("div");
  const spec = chartable(d.columns, d.rows);
  const tableView = buildTable(d.columns, d.rows);
  body.append(tableView);
  const seg = h("div", { class: "seg" });
  if (spec) {
    const bT = h("button", { class: "on", text: "Table" }), bC = h("button", { text: "Chart" });
    bT.onclick = () => { bT.classList.add("on"); bC.classList.remove("on"); body.replaceChildren(tableView); };
    bC.onclick = () => { bC.classList.add("on"); bT.classList.remove("on"); body.replaceChildren(buildChart(d.columns, d.rows, spec)); };
    seg.append(bT, bC);
  }
  return h("div", { class: "card" },
    h("div", { class: "card-h" }, h("span", { text: `Results · ${d.row_count}${d.truncated ? "+" : ""} rows` }),
      h("div", { class: "sp" }, spec ? seg : null,
        d.rows.length ? h("button", { class: "btn sm", text: "CSV", onclick: () => downloadCSV(d.columns, d.rows) }) : null)),
    body);
}

/* ───────────────────────── Turn lifecycle ───────────────────────── */
function newTurn(question) {
  $("#empty")?.remove();
  const steps = Object.fromEntries(STEPS.map(([k, label]) => [k, h("span", { class: "step", "data-k": k }, h("i", { class: "ico" }), label)]));
  const stepsRow = h("div", { class: "steps" }, Object.values(steps));
  const note = h("div", { class: "live-note", hidden: true });
  const out = h("div");
  const el = h("div", { class: "turn" }, h("div", { class: "q-bubble", text: question }), stepsRow, note, out);
  $("#feed").append(el);
  scrollDown();
  return { el, steps, stepsRow, note, out };
}
const scrollDown = () => requestAnimationFrame(() => { const f = $("#feed"); f.scrollTop = f.scrollHeight; });

function setStep(t, key, status, badge) {
  const s = t.steps[key]; if (!s) return;
  s.className = "step " + status;
  $$(".badge", s).forEach((b) => b.remove());
  if (badge) s.append(h("span", { class: "badge", text: badge }));
}

function renderResult(t, d) {
  Object.keys(t.steps).forEach((k) => setStep(t, k, "done", k === "generate" && d.attempts?.length ? `${d.attempts.length} fix${d.attempts.length > 1 ? "es" : ""}` : null));
  t.note.hidden = true;
  const parts = [];

  if (d.summary) parts.push(h("div", { class: "card" }, h("div", { class: "answer", text: d.summary })));

  // SQL card with Edit & run
  const pre = h("pre", { class: "sql" }); pre.innerHTML = highlightSQL(d.sql);
  const sqlBody = h("div"); sqlBody.append(pre);
  let currentSQL = d.sql, resultSlot = h("div");
  const editBtn = h("button", { class: "btn sm", text: "Edit & run" });
  editBtn.onclick = () => {
    const ta = h("textarea", { class: "sql-edit", spellcheck: "false" }); ta.value = currentSQL;
    const status = h("span", { class: "muted small" });
    const run = h("button", { class: "btn sm primary", text: "Run" });
    run.onclick = async () => {
      run.disabled = true; status.textContent = "Running…";
      try {
        const r = await api("/api/execute", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql: ta.value }) });
        currentSQL = r.sql; resultSlot.replaceChildren(buildResultsCard(r)); status.textContent = `OK · ${r.exec_ms} ms`;
      } catch (e) { status.textContent = "⚠ " + e.message; status.style.color = "var(--red)"; }
      run.disabled = false;
    };
    sqlBody.replaceChildren(ta, h("div", { class: "attempt" }, run, " ", status));
    editBtn.remove();
  };
  parts.push(h("div", { class: "card" },
    h("div", { class: "card-h" }, h("span", { text: "Generated SQL" }), h("div", { class: "sp" }, editBtn, copyBtn(() => currentSQL))),
    sqlBody));

  resultSlot.append(buildResultsCard(d));
  parts.push(resultSlot);

  if (d.attempts?.length) {
    parts.push(h("details", { class: "card" },
      h("summary", { class: "card-h", style: "cursor:pointer", text: `Self-correction · ${d.attempts.length} failed attempt${d.attempts.length > 1 ? "s" : ""}` }),
      d.attempts.map((a) => h("div", { class: "attempt" },
        h("div", { text: `Attempt ${a.attempt} · ${a.stage} error` }), h("div", { class: "err", text: a.error })))));
  }

  parts.push(h("div", { class: "meta" },
    d.cached ? h("span", { class: "m hit", text: "⚡ served from cache" }) : null,
    h("span", { class: "m" }, "latency ", h("b", { text: d.latency_ms + " ms" })),
    h("span", { class: "m" }, "query ", h("b", { text: d.exec_ms + " ms" })),
    h("span", { class: "m" }, "LLM calls ", h("b", { text: String(d.llm_calls) })),
    d.truncated ? h("span", { class: "m", text: "result truncated" }) : null));

  t.out.replaceChildren(...parts);
  state.conversation.push({ question: d.question, sql: d.sql });
  scrollDown();
}

function renderError(t, ev) {
  Object.values(t.steps).forEach((s) => { if (s.classList.contains("running")) s.className = "step failed"; });
  t.note.hidden = true;
  const parts = [h("div", { class: "card error-card" },
    h("div", { class: "card-h", text: "Couldn't answer that" }),
    h("div", { class: "error-body", text: ev.message }))];
  if (ev.sql) {
    const pre = h("pre", { class: "sql" }); pre.innerHTML = highlightSQL(ev.sql);
    parts.push(h("div", { class: "card" }, h("div", { class: "card-h", text: "Last attempted SQL" }), pre));
  }
  t.out.replaceChildren(...parts);
  scrollDown();
}

async function ask(question) {
  question = (question || "").trim();
  if (!question || state.busy) return;
  state.busy = true; $("#btn-send").disabled = true;
  $("#sidebar").classList.remove("open");
  const t = newTurn(question);
  const body = { question, conversation: $("#toggle-context").checked ? state.conversation.slice(-3) : [] };
  let finished = false;
  try {
    const resp = await fetch("/api/query/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!resp.ok) {
      let msg = resp.statusText;
      try { const j = await resp.json(); msg = typeof j.detail === "string" ? j.detail : "Invalid request."; } catch {}
      throw new Error(msg);
    }
    const reader = resp.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line) continue;
        const ev = JSON.parse(line);
        if (ev.type === "stage") {
          if (ev.stage === "cache") { Object.keys(t.steps).forEach((k) => setStep(t, k, "done")); }
          else if (ev.stage === "retry") { setStep(t, "generate", "running", `retry ${ev.attempt}`); setStep(t, "validate", "pending"); setStep(t, "execute", "pending"); }
          else setStep(t, ev.stage, ev.status === "running" ? "running" : "done");
        } else if (ev.type === "attempt") {
          t.note.hidden = false;
          t.note.textContent = `↻ attempt ${ev.attempt} failed at ${ev.stage}: ${ev.error} — asking the model to fix it…`;
          setStep(t, ev.stage === "validation" ? "validate" : "execute", "failed");
        } else if (ev.type === "result") { renderResult(t, ev.data); finished = true; }
        else if (ev.type === "error") { renderError(t, ev); finished = true; }
      }
    }
    if (!finished) renderError(t, { message: "The connection closed before an answer arrived." });
  } catch (e) {
    renderError(t, { message: e.message });
  } finally {
    state.busy = false; $("#btn-send").disabled = false; $("#input").focus();
    loadHistory(); loadStats();
  }
}

/* ───────────────────────── Composer & boot ───────────────────────── */
const input = $("#input");
const autosize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; };
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); const q = input.value; input.value = ""; autosize(); ask(q); });

$("#btn-new").onclick = () => {
  state.conversation = [];
  state.charts.forEach((c) => c.destroy()); state.charts.clear();
  $("#feed").replaceChildren(buildEmpty());
};

let examples = [];
function buildEmpty() {
  return h("div", { class: "empty", id: "empty" },
    h("h2", { text: "What do you want to know?" }),
    h("p", { text: "Your question is turned into PostgreSQL, validated against the live schema, run read-only, and explained back to you." }),
    h("div", { class: "chips" }, examples.map((q) => h("button", { class: "chip", text: q, onclick: () => ask(q) }))),
    h("div", { class: "upload-cta" },
      h("button", { class: "btn", type: "button", text: "⬆ Have your own data? Upload a CSV or Excel file", onclick: openUpload })));
}

async function refreshExamples() {
  try { examples = await api("/api/examples"); } catch {}
  const e = $("#empty");
  if (e) e.replaceWith(buildEmpty());
}

async function removeTable(name) {
  if (!confirm(`Remove the uploaded table "${name}"? Its data will be deleted from the database.`)) return;
  try {
    await api("/api/tables/" + encodeURIComponent(name), { method: "DELETE" });
    state.conversation = [];
    await loadSchema(true);
    refreshExamples();
  } catch (e) { alert("Could not remove the table: " + e.message); }
}

/* ───────────────────────── Upload dialog ───────────────────────── */
const modal = $("#modal"), modalBody = $("#modal-body");
// replaceChildren() would print the text "null" for optional (null) parts, so drop them first
const setBody = (...nodes) => modalBody.replaceChildren(...nodes.filter((x) => x != null && x !== false));
const fileSize = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
const guessTable = (name) => name.replace(/\.[^.]+$/, "").replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "my_data";

function openUpload() { modal.hidden = false; $("#sidebar").classList.remove("open"); renderPick(); }
function closeUpload() { modal.hidden = true; }
$("#btn-upload").onclick = openUpload;
$("#modal-close").onclick = closeUpload;
modal.addEventListener("mousedown", (e) => { if (e.target === modal) closeUpload(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) closeUpload(); });

function renderPick(error) {
  const input = h("input", { type: "file", accept: ".csv,.tsv,.txt,.xlsx,.xlsm", hidden: true });
  const drop = h("div", { class: "dropzone", tabindex: "0", role: "button" },
    h("div", { class: "dz-ico", text: "⬆" }),
    h("b", { text: "Drop a CSV or Excel file here" }),
    h("span", { class: "muted small", text: "or click to browse · .csv .tsv .xlsx · up to 25 MB" }));
  const pick = (f) => f && renderReady(f);
  drop.onclick = () => input.click();
  drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); };
  input.onchange = () => pick(input.files[0]);
  setBody(drop, input,
    error ? h("div", { class: "msg err", text: error }) : null,
    h("p", { class: "modal-note", text: "Your file becomes a table in PostgreSQL. After it loads you can ask questions about it right away. Excel files with several sheets create one table per sheet." }));
}

function renderReady(file, opts = {}) {
  const nameInput = h("input", { type: "text", value: opts.name || guessTable(file.name), maxlength: "50", spellcheck: "false" });
  const replace = h("input", { type: "checkbox", checked: opts.conflict ? "" : null });
  const upload = () => doUpload(file, nameInput.value.trim(), replace.checked);
  nameInput.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); upload(); } };
  setBody(
    h("div", { class: "file-chip" }, h("span", { text: "📄 " + file.name }), h("span", { class: "sz", text: fileSize(file.size) })),
    h("label", { class: "field" }, h("span", { text: "Table name (letters, numbers and underscores)" }), nameInput),
    opts.conflict ? h("div", { class: "msg warn", text: opts.conflict }) : null,
    opts.conflict ? h("label", { class: "check" }, replace, "Yes, replace the existing uploaded table") : null,
    opts.error ? h("div", { class: "msg err", text: opts.error }) : null,
    h("div", { class: "modal-actions" },
      h("button", { class: "btn ghost", type: "button", text: "Choose another file", onclick: () => renderPick() }),
      h("button", { class: "btn primary", type: "button", text: opts.conflict ? "Replace & upload" : "Upload", onclick: upload })));
  nameInput.focus(); nameInput.select();
}

async function doUpload(file, name, replace) {
  setBody(h("div", { class: "spin-wrap" }, h("div", { class: "spinner" }), h("div", { text: `Loading ${file.name} into PostgreSQL…` })));
  const fd = new FormData();
  fd.append("file", file);
  if (name) fd.append("table_name", name);
  fd.append("replace", replace ? "true" : "false");
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = data.detail;
      const msg = typeof d === "string" ? d : d?.message || "Upload failed.";
      if (d?.code === "exists_uploaded") return renderReady(file, { name, conflict: msg });
      return renderReady(file, { name, error: msg });
    }
    renderDone(data);
    state.conversation = [];
    await loadSchema(true);
    refreshExamples();
  } catch (e) {
    renderReady(file, { name, error: "Network error: " + e.message });
  }
}

function renderDone(data) {
  const blocks = data.tables.map((t) => h("div", { class: "done-t" },
    h("div", {}, "✓ ", h("b", { text: t.table }), ` — ${t.rows.toLocaleString()} rows · ${t.columns.length} columns`, t.replaced ? " (replaced)" : ""),
    h("div", { class: "col-chips" }, t.columns.slice(0, 14).map((c) => h("span", {}, c.name, h("i", { text: c.type }))),
      t.columns.length > 14 ? h("span", { text: `+${t.columns.length - 14} more` }) : null),
    t.notes.map((n) => h("div", { class: "muted small", style: "margin-top:6px", text: "ℹ " + n }))));
  setBody(
    ...blocks,
    data.suggestions.length ? h("div", { class: "try-h", text: "Try asking" }) : null,
    data.suggestions.length ? h("div", { class: "try-list" }, data.suggestions.map((q) => h("button", { class: "chip", type: "button", text: q, onclick: () => { closeUpload(); ask(q); } }))) : null,
    h("div", { class: "modal-actions" },
      h("button", { class: "btn ghost", type: "button", text: "Upload another", onclick: () => renderPick() }),
      h("button", { class: "btn primary", type: "button", text: "Done", onclick: closeUpload })));
}

(async function boot() {
  loadHealth(); loadSchema(); loadStats();
  try { examples = await api("/api/examples"); } catch {}
  $("#feed").replaceChildren(buildEmpty());
  setInterval(loadHealth, 30000);
})();
