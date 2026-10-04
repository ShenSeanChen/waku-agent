// waku dashboard — the five fenced blocks of a research report, drawn in chat.
//
// A report (`waku-report v1`, spec 007 A; skills/research-report/SKILL.md)
// carries its numbers, chart, comparison, timeline and sources as fenced
// blocks whose body is JSON:
//
//   waku-metrics   [{label, value, note?}]
//   waku-chart     {type: "bar"|"line", title, unit?, series: [{label, value}]}
//   waku-compare   {columns: [str], rows: [{name, cells: [str|bool|null]}]}
//   waku-timeline  [{date: "YYYY-MM-DD"|"YYYY-MM", event, subject?}]
//   waku-sources   [{title, url?, via?, cost_usd?}]
//
// waku.one draws them (its components/report); this is the chat's version,
// smaller and with no chart library. renderMarkdown (util.js) hands every
// closed `waku-*` fence to reportBlock, which answers HTML, or null when the
// block is one it does not know or cannot read: then the fence is drawn as
// code, exactly as before this file existed. Nothing here throws on a block.
//
// EVERY BODY IS UNTRUSTED. The model wrote it, often from web pages it read.
// Every string goes through esc(), and a link is drawn only to an http or
// https address; anything else stays text.

const REPORT_BLOCKS = {
  "waku-metrics": ["Numbers", metricsBlock],
  "waku-chart": ["Chart", chartBlock],
  "waku-compare": ["Comparison", compareBlock],
  "waku-timeline": ["Timeline", timelineBlock],
  "waku-sources": ["Sources", sourcesBlock],
};

// raw: the fence's body as the model wrote it, not yet escaped.
function reportBlock(lang, raw){
  const known = Object.prototype.hasOwnProperty.call(REPORT_BLOCKS, lang) ? REPORT_BLOCKS[lang] : null;
  if (!known) return null;
  let body;
  try { body = known[1](JSON.parse(raw)); } catch (e) { return null; }
  if (!body) return null;
  // The raw JSON rides along hidden, so copyCode (util.js) copies it as before.
  const copy = uiButton("Copy", {level: "tertiary", size: "sm", cls: "mdcode-copy", onclick: "copyCode(this)"});
  return `<div class="mdcode rblock rblock-${lang.slice(5)}"><div class="mdcode-head"><span class="mdcode-lang">${known[0]}</span>${copy}</div>`
    + `<div class="rblock-body">${body}</div><pre hidden><code>${esc(raw)}</code></pre></div>`;
}

// --- reading the shapes. Each check returns null for a value it cannot use,
// and a block with one bad item is not drawn at all: a half-drawn table would
// hide that something is missing.
const rbObj = v => typeof v === "object" && v !== null && !Array.isArray(v);
const rbList = v => Array.isArray(v) && v.length > 0 && v.every(rbObj);
const rbText = v => typeof v === "string" && v.trim() !== "";
const rbOpt = v => v === undefined || v === null || typeof v === "string";
const rbNum = v => typeof v === "number" && Number.isFinite(v);

// A link only to http or https. A model wrote the address, so `javascript:`,
// `data:` or a relative path is refused, and the title is drawn as text.
function safeHref(url){
  if (typeof url !== "string") return null;
  try {
    const u = new URL(url.trim());
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch (e) { return null; }
}

// 41 with "USD M" -> "41 USD M"; with "$M" -> "$41M"; with "%" -> "41%".
function withUnit(value, unit){
  const abs = Math.abs(value);
  const n = abs.toLocaleString("en-GB", {maximumFractionDigits: 2});
  const sign = value < 0 ? "-" : "";
  if (!rbText(unit)) return sign + n;
  if (/^[$€£¥]/.test(unit)) return sign + unit[0] + n + unit.slice(1);
  if (/^(%|k|K|M|B|bn)$/.test(unit)) return sign + n + unit;
  return `${sign}${n} ${unit}`;
}

// "2026-10-03" -> "3 Oct 2026", "2026-10" -> "Oct 2026", read as written.
const RB_DATE = /^\d{4}-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?$/;
const RB_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function timelineDate(date){
  const [y, m, d] = date.split("-");
  const month = RB_MONTHS[Number(m) - 1];
  return d ? `${Number(d)} ${month} ${y}` : `${month} ${y}`;
}

// --- the five blocks. Each takes the parsed JSON and answers HTML or null.

function metricsBlock(json){
  if (!rbList(json)) return null;
  const tiles = json.map(m => {
    const value = rbNum(m.value) ? String(m.value) : m.value;
    if (!rbText(m.label) || !rbText(value) || !rbOpt(m.note)) return null;
    return `<div class="rb-metric"><span class="rb-label">${esc(m.label)}</span>`
      + `<span class="rb-value">${esc(value)}</span>`
      + (rbText(m.note) ? `<span class="rb-note">${esc(m.note)}</span>` : "") + `</div>`;
  });
  return tiles.includes(null) ? null : `<div class="rb-metrics">${tiles.join("")}</div>`;
}

// A bar per point, its length a share of the largest. A line chart is drawn
// the same way, in the order written; a negative value draws no bar.
function chartBlock(json){
  if (!rbObj(json) || (json.type !== "bar" && json.type !== "line")) return null;
  if (!rbText(json.title) || !rbOpt(json.unit) || !rbList(json.series)) return null;
  if (!json.series.every(p => rbText(p.label) && rbNum(p.value))) return null;
  const peak = Math.max(0, ...json.series.map(p => p.value));
  const rows = json.series.map(p => {
    const share = peak > 0 ? Math.round(Math.max(p.value, 0) / peak * 1000) / 10 : 0;
    return `<div class="rb-bar-row"><span class="rb-bar-label">${esc(p.label)}</span>`
      + `<span class="rb-bar"><span class="rb-bar-fill" style="width:${share}%"></span></span>`
      + `<span class="rb-bar-value">${esc(withUnit(p.value, json.unit))}</span></div>`;
  }).join("");
  const unit = rbText(json.unit) ? `<span class="rb-note">${esc(json.unit)}</span>` : "";
  return `<div class="rb-chart-title">${esc(json.title)}${unit}</div><div class="rb-bars">${rows}</div>`;
}

// Yes, No, Unknown or the text: each mark is a word, never colour alone.
function compareMark(cell){
  if (cell === true) return `<span class="rb-yes">Yes</span>`;
  if (cell === false) return `<span class="rb-no">No</span>`;
  if (cell === null) return `<span class="rb-unknown">Unknown</span>`;
  return esc(cell);
}

// Things down the side, questions across the top. It never stacks: in a
// narrow chat it scrolls sideways in its own box, the names pinned at left.
function compareBlock(json){
  if (!rbObj(json) || !Array.isArray(json.columns) || !json.columns.length) return null;
  if (!json.columns.every(c => typeof c === "string") || !rbList(json.rows)) return null;
  const rows = json.rows.map(r => {
    if (!rbText(r.name) || !Array.isArray(r.cells) || r.cells.length > json.columns.length) return null;
    const cells = r.cells.map(c => rbNum(c) ? String(c) : c);
    if (!cells.every(c => c === null || typeof c === "boolean" || typeof c === "string")) return null;
    while (cells.length < json.columns.length) cells.push(null);   // a short row: unknown
    return [esc(r.name), ...cells.map(compareMark)];
  });
  if (rows.includes(null)) return null;
  return `<div class="rb-compare">${uiTable([`<span class="rb-sr">Name</span>`, ...json.columns.map(esc)], rows)}</div>`;
}

function timelineBlock(json){
  if (!rbList(json)) return null;
  const items = json.map(t => {
    if (!rbText(t.date) || !RB_DATE.test(t.date) || !rbText(t.event) || !rbOpt(t.subject)) return null;
    return `<li><span class="rb-date">${esc(timelineDate(t.date))}</span><span class="rb-event">`
      + (rbText(t.subject) ? `<span class="rb-subject">${esc(t.subject)}</span> ` : "")
      + `${esc(t.event)}</span></li>`;
  });
  return items.includes(null) ? null : `<ol class="rb-timeline">${items.join("")}</ol>`;
}

// Numbered, so the report's text can say [2]. Under each title one faint line:
// the host, the tool that found it, what that call cost.
function sourcesBlock(json){
  if (!rbList(json)) return null;
  const money = c => "$" + Number(c.toPrecision(2));
  let spent = 0, paid = 0;
  const items = json.map(s => {
    const cost = s.cost_usd;
    if (!rbText(s.title) || !rbOpt(s.url) || !rbOpt(s.via)) return null;
    if (cost !== undefined && cost !== null && !(rbNum(cost) && cost >= 0)) return null;
    const href = safeHref(s.url);
    const title = href
      ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a>`
      : `<span>${esc(s.title)}</span>`;
    const facts = [];
    if (href) facts.push(new URL(href).hostname.replace(/^www\./, ""));
    if (rbText(s.via)) facts.push(s.via);
    if (rbNum(cost)){ facts.push(money(cost)); spent += cost; paid++; }
    return `<li>${title}${facts.length ? `<span class="rb-note">${esc(facts.join(" · "))}</span>` : ""}</li>`;
  });
  if (items.includes(null)) return null;
  const total = paid ? `<div class="rb-note">Spent ${money(spent)} on ${paid} paid ${paid === 1 ? "call" : "calls"}.</div>` : "";
  return `<ol class="rb-sources">${items.join("")}</ol>${total}`;
}
