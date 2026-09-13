// Waku Memory's primitives, as functions that return HTML strings — the way
// every view here already builds its markup. Names follow the Memory
// console's components/ui with a "ui" prefix, because js/ shares one global
// scope. Each primitive's look lives in style.css under its class; a view
// calls the function instead of writing the markup. See static/README.md.

// Card — paper ground, a hairline, 16px padding (12px when size is "sm").
function uiCard(body, {title = "", action = "", footer = "", size = "", cls = ""} = {}){
  const head = (title || action)
    ? `<div class="card-head">${title ? `<div class="card-title">${title}</div>` : ""}${action ? `<div class="card-action">${action}</div>` : ""}</div>`
    : "";
  return `<div class="card${size === "sm" ? " card-sm" : ""}${cls ? " " + cls : ""}">${head}<div class="card-body">${body}</div>${footer ? `<div class="card-foot">${footer}</div>` : ""}</div>`;
}

// Badge — the 20px chip. variant: neutral | ok | warn | bad | live | miss | value.
// "value" is for data (a grade, a cost, a model id): normal case, no tracking.
function uiBadge(text, variant = "neutral", title = ""){
  return `<span class="badge badge-${variant}"${title ? ` title="${esc(title)}"` : ""}>${text}</span>`;
}

// Table — columns: [labelHtml, …]; rows: [[cellHtml, …], …]. Cells are HTML,
// so a caller can put a badge or a link in one.
function uiTable(columns, rows, {caption = "", empty = "nothing yet"} = {}){
  if (!rows.length) return uiCard(`<span class="empty">${empty}</span>`);
  const head = `<tr>${columns.map(c => `<th>${c}</th>`).join("")}</tr>`;
  const body = rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("");
  return `<div class="tbl-wrap"><table class="tbl">${head}${body}</table></div>${caption ? `<div class="tbl-caption">${caption}</div>` : ""}`;
}

// Tabs, Memory's line variant — items: [{label, href, on, count}].
function uiTabs(items){
  return `<div class="tabs" role="tablist">${items.map(t =>
    `<a class="tab${t.on ? " on" : ""}" role="tab" aria-selected="${t.on ? "true" : "false"}" href="${t.href}">${t.label}${t.count != null && t.count !== "" ? `<span class="tab-n">${t.count}</span>` : ""}</a>`).join("")}</div>`;
}

// Notice — level: note | ok | warn | failed. The level word is the mark.
function uiNotice(level, html, action = ""){
  const role = level === "failed" ? ' role="alert"' : "";
  return `<div class="notice notice-${level}"${role}><span class="notice-mark">${level}</span><div class="notice-body">${html}</div>${action ? `<div class="notice-action">${action}</div>` : ""}</div>`;
}

// Stat band — items: [{label, value, sub, tone}]; tone "ok" colours the value.
function uiStatBand(items){
  return `<div class="stat-band">${items.map(s =>
    `<div class="stat"><span class="stat-label">${s.label}</span><b class="stat-value${s.tone ? " " + s.tone : ""}">${s.value}</b>${s.sub ? `<span class="stat-sub">${s.sub}</span>` : ""}</div>`).join("")}</div>`;
}

// Row — a lead (a badge, a date) beside a title and a meta line.
function uiRow(lead, title, meta = "", {onclick = "", cls = ""} = {}){
  return `<div class="list-row${cls ? " " + cls : ""}"${onclick ? ` onclick="${onclick}"` : ""}><div class="list-lead">${lead}</div><div class="list-body"><div class="list-title">${title}</div>${meta ? `<div class="list-meta">${meta}</div>` : ""}</div></div>`;
}

// Dialog — one native <dialog> at a time. Escape closes it natively, and a
// click on the scrim closes it too. onClose runs after it closes, however
// that happened, so a caller can tidy up in one place.
let _dialogClose = null;
function openDialog(html, {wide = false, onClose = null, label = ""} = {}){
  closeDialog();
  const d = document.createElement("dialog");
  d.className = "dialog" + (wide ? " dialog-wide" : "");
  if (label) d.setAttribute("aria-label", label);
  d.innerHTML = html;
  d.addEventListener("click", e => { if (e.target === d) closeDialog(); });
  d.addEventListener("close", () => { d.remove(); const f = _dialogClose; _dialogClose = null; if (f) f(); });
  _dialogClose = onClose;
  document.body.appendChild(d);
  d.showModal();
  const first = d.querySelector("input,select,textarea,button");
  if (first) first.focus();
  return d;
}
function closeDialog(){
  const d = document.querySelector("dialog.dialog[open]");
  if (d) d.close();
}
