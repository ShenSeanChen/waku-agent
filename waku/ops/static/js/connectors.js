// The parts of the Connections page that belong to a server a person
// connected themselves (spec 025, moved here from Tools by spec 030): the
// sign-in link while one waits, the dialog a card's Manage button opens, and
// the form that adds a server by its address ("For developers").
//
// The rows come from `connectors` in /api/data. The page takes a starter name
// or an address only. A server that is a program on this machine is added from the command
// line (`waku mcp add <name> -- <command>`), never from a page.
//
// A name, an address, an account and a sign-in link are text from outside,
// so every one goes through esc, and a link is shown only when it is http(s).

let connectorNote = "";
// Whether the "For developers" section is open. The page is drawn again
// every few seconds, and a section a person opened must stay open.
let connectionsDevOpen = false;

function connectorLink(link){
  return /^https?:\/\//.test(link || "")
    ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(link)}</a>` : "";
}

// The note a person reads after Connect, Remove or the writes switch, and the
// sign-in link while a server waits for one. Drawn above the cards.
function connectorSignIn(s){
  const note = connectorNote ? `<div class="meta" style="margin-bottom:var(--space-2)">${esc(connectorNote)}</div>` : "";
  if (!s || !connectorLink(s.link)) return note;
  return note + uiNotice("note", `<b>Sign in to ${esc(s.name)}.</b>
    <div class="r">Open this link on the computer Waku runs on. It does not work from another device.</div>
    <div class="r" style="word-break:break-all">${connectorLink(s.link)}</div>`);
}

function connectorServer(name){
  return (((D || {}).connectors || {}).servers || []).find(r => r.name === name);
}

// What a card's Manage button opens: the server's address and account, how
// many tools it brought, the switch for its writes, and Remove.
function openConnectorManage(name){
  const r = connectorServer(name);
  if (!r) return;
  const tools = r.tools === null ? "Its tools are counted after the next message."
    : `${r.tools} tool${r.tools === 1 ? "" : "s"}${r.asks ? ", " + r.asks + " ask first" : ""}.`;
  const writes = r.switch
    ? uiButton(r.writes === "allow" ? "Writes run without asking" : "Writes ask first",
        {level: "secondary", onclick: `setConnectorWrites('${esc(r.name)}', ${r.writes !== "allow"})`,
         title: r.writes === "allow" ? "Make " + r.name + " ask before it writes"
                                     : "Let " + r.name + " write without asking"}) : "";
  const remove = r.removable
    ? uiButton("Remove", {level: "destructive", onclick: `removeConnector('${esc(r.name)}')`, title: "Remove " + r.name})
    : `<span class="meta">It came with the ${esc(r.plugin)} plugin, and it goes when the plugin goes.</span>`;
  openDialog(`<div>
      <header class="connmodal-head">
        <div class="connmodal-title"><h3>${esc(r.name)}</h3></div>
        ${uiButton("Close", {level: "tertiary", size: "sm", cls: "connmodal-close", onclick: "closeDialog()", attrs: 'aria-label="Close"'})}
      </header>
      <p class="conndesc connmodal-desc" style="word-break:break-all">${esc(r.address)}</p>
      <div class="connmodal-meta"><span>${r.account ? esc(r.account) : ""}</span><span>${tools}</span></div>
      <div class="dialog-foot">${writes}${remove}</div>
    </div>`, {label: r.name});
}

// Inside "For developers" at the foot of the page: a server that is not on
// the starter list, by its address.
function connectorAddSection(){
  return `<h3 class="connsub">Add a server by address</h3>
    <div class="connmore-body">
      <div class="meta" style="margin-bottom:var(--space-2)">For an app that is not listed above and gives
        its tools from a web address. Waku asks before one of its tools changes anything.</div>
      <div style="display:flex;gap:var(--space-2);align-items:center">
        <input id="connector-url" type="url" placeholder="https://mcp.example.com/mcp"
          style="flex:1;min-width:0" onfocus="markEditing()" oninput="markEditing()"
          onkeydown="if(event.key==='Enter')addConnector()">
        ${uiButton("Add", {level: "primary", size: "sm", onclick: "addConnector()"})}
      </div>
      <div class="meta" style="margin-top:var(--space-2)">A server that is a program on this machine is added
        from the command line: <code>waku mcp add &lt;name&gt; -- &lt;command&gt;</code>.
        A plugin brings its server and its ${uiLink("hooks", "#tools/hooks")} with it:
        <code>waku plugin install &lt;name&gt;@&lt;marketplace&gt;</code>.
        Restart the dashboard to load a new server's tools.</div>
    </div>`;
}

// With a name: a starter card's Connect button. Without one: the address in
// the field.
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
  closeDialog();
  await refresh();
}

// The switch in the Manage dialog: let this connector's tools write without
// asking, or make its writes ask again.
async function setConnectorWrites(name, allow){
  const r = await postJSON("/api/connectors/approve", {name, allow});
  connectorNote = r.error ? ("Not changed: " + r.error)
    : (allow ? name + " now writes without asking." : name + " asks before it writes.");
  closeDialog();
  await refresh();
}
