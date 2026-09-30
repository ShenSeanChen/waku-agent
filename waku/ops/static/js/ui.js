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
  // .badge-t is the text's own box: it is what a capped badge ellipsizes.
  return `<span class="badge badge-${variant}"${title ? ` title="${esc(title)}"` : ""}><span class="badge-t">${text}</span></span>`;
}

// Table — columns: [labelHtml, …]; rows: [[cellHtml, …], …]. Cells are HTML,
// so a caller can put a badge or a link in one. A row can also be
// {id, cells}, for code that finds its row by id (editFact in memory.js).
function uiTable(columns, rows, {caption = "", empty = "nothing yet"} = {}){
  if (!rows.length) return uiCard(`<span class="empty">${empty}</span>`);
  const head = `<tr>${columns.map(c => `<th>${c}</th>`).join("")}</tr>`;
  const body = rows.map(r => {
    const cells = Array.isArray(r) ? r : r.cells;
    const id = Array.isArray(r) || !r.id ? "" : ` id="${esc(r.id)}"`;
    return `<tr${id}>${cells.map(c => `<td>${c}</td>`).join("")}</tr>`;
  }).join("");
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
// that happened, so a caller can tidy up in one place. Each dialog keeps its
// own onClose: the close event of a dialog being replaced fires after the
// next one has opened, so a shared variable would hand it the wrong one.
// The content sits in .dialog-in, which carries the padding, so a click that
// lands on the <dialog> element itself can only be a click on the scrim.
function openDialog(html, {wide = false, onClose = null, label = ""} = {}){
  closeDialog();
  const d = document.createElement("dialog");
  d.className = "dialog" + (wide ? " dialog-wide" : "");
  if (label) d.setAttribute("aria-label", label);
  d.innerHTML = `<div class="dialog-in">${html}</div>`;
  d.addEventListener("click", e => { if (e.target === d) d.close(); });
  d.addEventListener("close", () => { d.remove(); if (onClose) onClose(); });
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

// Button — Memory's four levels: primary (the main action), secondary,
// tertiary (text only) and destructive; size "sm" for a row of small ones.
// danger colours a tertiary action red (delete in a table row). cls adds a
// class that other code finds the button by. onclick is an inline handler,
// so it must not contain a double quote.
function uiButton(label, {level = "secondary", size = "", onclick = "", title = "", danger = false, cls = "", attrs = ""} = {}){
  const classes = `btn btn-${level}${size === "sm" ? " btn-sm" : ""}${danger ? " btn-danger" : ""}${cls ? " " + cls : ""}`;
  return `<button type="button" class="${classes}"${onclick ? ` onclick="${onclick}"` : ""}${title ? ` title="${esc(title)}"` : ""}${attrs ? " " + attrs : ""}>${label}</button>`;
}

// Link — only for going somewhere (another tab). An action is a button.
function uiLink(label, href, {title = ""} = {}){
  return `<a class="link" href="${href}"${title ? ` title="${esc(title)}"` : ""}>${label}</a>`;
}

// Menu — one floating menu at a time, under the control that opened it. A
// click outside or Escape closes it and gives focus back to that control;
// the arrow keys move between items. Build the contents with uiMenuLabel,
// uiMenuItem and uiMenuSep.
let _menuTrigger = null;
function openMenu(trigger, html, {width = "", align = "right"} = {}){
  closeMenu();
  const m = document.createElement("div");
  m.className = "menu"; m.id = "ui-menu"; m.setAttribute("role", "menu");
  if (width) m.style.width = width;
  m.innerHTML = html;
  document.body.appendChild(m);
  const r = trigger.getBoundingClientRect();
  const gap = 6;
  const left = align === "right" ? r.right - m.offsetWidth : r.left;
  m.style.top = (r.bottom + gap) + "px";
  m.style.left = Math.max(gap, Math.min(left, innerWidth - m.offsetWidth - gap)) + "px";
  _menuTrigger = trigger;
  const first = m.querySelector(".menu-item");
  if (first) first.focus();
  setTimeout(() => document.addEventListener("click", _menuOutside), 0);
  document.addEventListener("keydown", _menuKey);
  return m;
}
function closeMenu(){
  const m = document.getElementById("ui-menu");
  if (!m) return;
  m.remove();
  document.removeEventListener("click", _menuOutside);
  document.removeEventListener("keydown", _menuKey);
  if (_menuTrigger && document.contains(_menuTrigger)) _menuTrigger.focus();
  _menuTrigger = null;
}
function _menuOutside(e){
  const m = document.getElementById("ui-menu");
  if (m && !m.contains(e.target) && !(_menuTrigger && _menuTrigger.contains(e.target))) closeMenu();
}
function _menuKey(e){
  const m = document.getElementById("ui-menu");
  if (!m) return;
  if (e.key === "Escape"){ e.preventDefault(); closeMenu(); return; }
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  const items = [...m.querySelectorAll(".menu-item")];
  if (!items.length) return;
  const i = items.indexOf(document.activeElement);
  const next = e.key === "ArrowDown" ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
  items[next].focus();
  e.preventDefault();
}
function uiMenuLabel(text){ return `<div class="menu-label">${text}</div>`; }
function uiMenuItem(html, {onclick = "", on = false, sub = "", danger = false, title = ""} = {}){
  return `<button type="button" class="menu-item${on ? " on" : ""}${danger ? " danger" : ""}" role="menuitem"${onclick ? ` onclick="${onclick}"` : ""}${title ? ` title="${esc(title)}"` : ""}>${html}${sub ? `<span class="menu-sub">${sub}</span>` : ""}</button>`;
}
function uiMenuSep(){ return `<div class="menu-sep" role="separator"></div>`; }
