// waku dashboard — escaping, markdown, core globals (D/editing), postJSON, reveal.
// Split out of app.js: classic <script>, shared global scope (no build
// step, no modules). Load order + rules: static/README.md.

// Escapes quotes too, not just &<>. Text nodes never needed it, and for a long
// time nothing put model output inside an ATTRIBUTE — so the gap was invisible.
// The copy buttons (#58) are the first place that happens, and a reply
// containing one double quote was enough to close data-text="..." and attach
// its own event handler. The model's output is not fully ours: search_web and
// browse_web pull text off the open web, and this dashboard holds the memory,
// the traces and the settings. Escape at the helper, once, for every caller.
const esc = s => (s??"").toString().replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// --- tiny markdown renderer for chat replies (no dependency, XSS-safe: we
// escape first, then apply a small set of transforms the LLM actually uses:
// bold/italic/code, links, ordered/unordered lists, and tables).
function mdInline(s){   // s is already HTML-escaped
  return s
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|message:\/\/[^\s)]+)\)/g,
             (m, text, url) => `<a href="${url}" target="_blank" rel="noopener">${text}</a>`)
    .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*_`])[*_]([^*_`\s][^*_`]*?)[*_](?![\w*])/g, "$1<em>$2</em>")
    .replace(/`([^`]+?)`/g, "<code>$1</code>");
}
function renderMarkdown(text){
  const lines = esc(text).split(/\r?\n/);
  const row = l => /^\s*\|.*\|\s*$/.test(l);
  const sep = l => /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(l);
  const cells = l => l.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
  const out = [];
  let i = 0;
  while (i < lines.length){
    const l = lines[i];
    if (row(l) && i+1 < lines.length && sep(lines[i+1])){          // table
      const head = cells(l); i += 2; const body = [];
      while (i < lines.length && row(lines[i])){ body.push(cells(lines[i])); i++; }
      out.push(`<table class="mdtable"><thead><tr>${head.map(h=>`<th>${mdInline(h)}</th>`).join("")}</tr></thead><tbody>${
        body.map(r=>`<tr>${r.map(c=>`<td>${mdInline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    const h = l.match(/^\s*#{1,6}\s+(.*)$/);
    if (h){ out.push(`<div class="mdh">${mdInline(h[1])}</div>`); i++; continue; }
    if (/^\s*[-*]\s+/.test(l)){                                     // unordered list
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])){ items.push(mdInline(lines[i].replace(/^\s*[-*]\s+/,""))); i++; }
      out.push(`<ul class="mdlist">${items.map(x=>`<li>${x}</li>`).join("")}</ul>`); continue;
    }
    if (/^\s*\d+\.\s+/.test(l)){                                    // ordered list
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])){ items.push(mdInline(lines[i].replace(/^\s*\d+\.\s+/,""))); i++; }
      out.push(`<ol class="mdlist">${items.map(x=>`<li>${x}</li>`).join("")}</ol>`); continue;
    }
    if (/^\s*`{3,}/.test(l)){                                       // fenced code block
      const lang = l.replace(/^\s*`{3,}/, "").trim();
      i++;
      const codeLines = [];
      while (i < lines.length && !/^\s*`{3,}\s*$/.test(lines[i])){ codeLines.push(lines[i]); i++; }
      if (i < lines.length) i++;   // skip closing ```
      const langLabel = lang ? `<span class="mdcode-lang">${lang}</span>` : "";
      out.push(`<div class="mdcode"><div class="mdcode-head">${langLabel}<button class="mdcode-copy" onclick="copyCode(this)">Copy</button></div><pre><code>${codeLines.join("\n")}</code></pre></div>`);
      continue;
    }
    if (/^\s*[-*_]{3,}\s*$/.test(l)){ out.push("<hr class='mdhr'>"); i++; continue; } // hr
    if (/^\s*$/.test(l)){ i++; continue; }
    const para = [];                                                // paragraph
    while (i < lines.length && lines[i].trim() && !/^\s*[-*]\s|^\s*\d+\.\s|^\s*#{1,6}\s/.test(lines[i])
           && !(row(lines[i]) && i+1<lines.length && sep(lines[i+1]))){
      para.push(mdInline(lines[i])); i++;
    }
    out.push(`<div class="mdp">${para.join("<br>")}</div>`);
  }
  return out.join("");
}
function copyCode(btn){
  const code = btn.closest(".mdcode").querySelector("pre code");
  navigator.clipboard.writeText(code.textContent).then(() => {
    const orig = btn.textContent;
    btn.textContent = "Copied!"; btn.classList.add("copied");
    setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 2000);
  });
}
function copyMsg(btn){
  const text = btn.getAttribute("data-text") || "";
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = "Copied!"; btn.classList.add("copied");
    setTimeout(() => { btn.textContent = orig; btn.classList.remove("copied"); }, 2000);
  });
}
let D = null;

// Click a section's data to open the real local file/folder (editor or Finder).
function revealFile(p){ fetch("/api/reveal?path=" + encodeURIComponent(p)); }
const reveal = (path, label) => `<a class="reveal" onclick="revealFile('${path}')">${esc(label)}</a>`;

// --- memory CRUD (dashboard side). `editing` pauses the 5s rebuild so an
// in-progress edit isn't wiped (same idea as the animation guard).
let editing = false;
async function postJSON(url, body){ return (await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json(); }

// --- Shared row atoms.
//
// Deliberately three tiny FRAGMENTS, not one big sessionRow()/pinnedRow().
// The session inbox (views.js) and the dock's thread menu (dock.js) draw
// genuinely different things — a card in a tab versus an item in a dropdown —
// so a shared row component would need a parameter for every difference and
// would be worse than the duplication. What they actually share is these three
// facts about a session, and those are the parts that drift: add a gateway and
// the tag strip changes in two places; change the date format and the meta line
// changes in two places. That has already happened once in this repo.

// The channel tags on a conversation (web / telegram / voice / cli / discord).
const gwTags = s => (s.sources||[]).map(src =>
  `<span class="gwtag ${esc(src)}">${esc(src)}</span>`).join("");

// "12 msg · 2026-07-26 21:56" — a session's size and when it last moved.
const sessionMeta = s =>
  `${s.messages} msg · ${esc((s.last_at||"").slice(0,16).replace("T"," "))}`;

// One tool in a stage strip. Shared by the chat dock's harness strip
// (render.js) and the arena's per-card strip (compare.js) — those two strips
// are otherwise different on purpose (the arena has no gate/reply stage and
// wraps), but the chip itself must look identical in both or the same tool
// call appears to be two different things.
const toolChip = name => `<span class="stage done">tool · ${esc(name)}</span>`;
