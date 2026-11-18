// The "Connectors" card on Tools > MCP (spec 025): every MCP server Waku
// would connect, with its account and its tool count, a field that adds one
// by its address, one button for each entry of the starter list, and a
// Remove button on each server a person added.
//
// The rows come from `connectors` in /api/data. The card takes a starter name
// or an address only. A server that is a program on this machine is added from the command
// line (`waku mcp add <name> -- <command>`), never from a page.
//
// A name, an address, an account and a sign-in link are text from outside,
// so every one goes through esc, and a link is shown only when it is http(s).

let connectorNote = "";

function connectorLink(link){
  return /^https?:\/\//.test(link || "")
    ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(link)}</a>` : "";
}

function connectorSignIn(c){
  const s = c.sign_in;
  if (!s || !connectorLink(s.link)) return "";
  return uiNotice("note", `<b>Sign in to ${esc(s.name)}.</b>
    <div class="r">Open this link on the computer Waku runs on. It does not work from another device.</div>
    <div class="r" style="word-break:break-all">${connectorLink(s.link)}</div>`);
}

function connectorRows(c){
  if (!c.servers.length) return `<div class="meta">No server yet.</div>`;
  return c.servers.map(r => uiRow(uiBadge(esc(r.name), "value"),
    `${esc(r.address)}${r.account ? `<div class="meta">${esc(r.account)}</div>` : ""}`,
    (r.tools === null ? "" : `<span class="meta">${r.tools} tool${r.tools === 1 ? "" : "s"}</span> `) +
    (r.removable
      ? uiButton("Remove", {level: "tertiary", size: "sm", danger: true,
          onclick: `removeConnector('${esc(r.name)}')`, title: "Remove " + r.name})
      : `<span class="meta">from the ${esc(r.plugin)} plugin</span>`))).join("");
}

function connectorStarter(c){
  const rest = (c.starter || []).filter(s => !s.added);
  if (!rest.length) return "";
  return `<div class="meta" style="margin:var(--space-3) 0 calc(var(--spacing) * 1)">Add one from the starter list</div>
    <div style="display:flex;gap:var(--space-2);flex-wrap:wrap">${rest.map(s =>
      uiButton(esc(s.title), {level: "secondary", size: "sm",
        onclick: `addConnector('${esc(s.name)}')`, title: "Connect " + s.title})).join("")}</div>`;
}

function connectorsCard(d){
  const c = d.connectors || {available: false, starter: [], servers: [], sign_in: null};
  if (!c.available) return "";
  const note = connectorNote ? `<div class="meta" style="margin-bottom:var(--space-2)">${esc(connectorNote)}</div>` : "";
  return uiCard(note + connectorSignIn(c) + connectorRows(c) + connectorStarter(c) + `
    <div class="meta" style="margin:var(--space-3) 0 calc(var(--spacing) * 1)">Add a server by its address</div>
    <div style="display:flex;gap:var(--space-2);align-items:center">
      <input id="connector-url" type="url" placeholder="https://mcp.example.com/mcp"
        style="flex:1" onfocus="markEditing()" oninput="markEditing()"
        onkeydown="if(event.key==='Enter')addConnector()">
      ${uiButton("Add", {level: "primary", size: "sm", onclick: "addConnector()"})}
    </div>
    <div class="meta" style="margin-top:var(--space-2)">A server that is a program on this machine is added
      from the command line: <code>waku mcp add &lt;name&gt; -- &lt;command&gt;</code>.
      Restart the dashboard to load a new server's tools.</div>`,
    {title: "Connectors"});
}

// With a name: a starter button. Without one: the address in the field.
async function addConnector(name){
  const field = document.getElementById("connector-url");
  const url = field ? field.value.trim() : "";
  if (!name && !url) return;
  const r = await postJSON("/api/connectors/add", name ? {name} : {url});
  connectorNote = r.error ? ("Not added: " + r.error) : (r.message || ("Added " + r.name + "."));
  editing = false;
  await refresh();
}

async function removeConnector(name){
  const r = await postJSON("/api/connectors/remove", {name});
  connectorNote = r.error ? ("Not removed: " + r.error) : ("Removed " + name + ".");
  await refresh();
}
