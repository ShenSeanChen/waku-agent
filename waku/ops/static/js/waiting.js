// The "Waiting" card on Overview (spec 024): the timers that have not fired,
// the calls that wait for a yes, and the messages that are queued or could
// not be sent, each row with a Cancel button.
//
// The rows come from `waiting` in /api/data, which reads the same three
// tables as `waku waiting`. A note, a call and a message are text someone
// else wrote, so every one goes through esc.

let waitingNote = "";

function waitingWhen(stamp){
  const t = new Date(stamp);
  return isNaN(t) ? esc(stamp) : esc(t.toLocaleString([], {weekday:"short", day:"numeric", month:"short", hour:"2-digit", minute:"2-digit"}));
}

function waitingRows(label, items, lead){
  if (!items.length) return "";
  return `<div class="meta" style="margin:var(--space-2) 0 calc(var(--spacing) * 0.5)">${label}</div>` +
    items.map(i => uiRow(uiBadge(esc(i.id), "value"),
      esc(i.what),
      `${lead(i)} ${uiButton("Cancel", {level: "tertiary", size: "sm", danger: true,
        onclick: `cancelWaiting('${esc(i.id)}')`, title: "Cancel " + i.id})}`)).join("");
}

function waitingCard(d){
  const w = d.waiting || {timers: [], calls: [], messages: []};
  const note = waitingNote ? `<div class="meta">${esc(waitingNote)}</div>` : "";
  if (!(w.timers.length + w.calls.length + w.messages.length))
    return note ? uiCard(note, {title: "Waiting"}) : "";
  return uiCard(note +
    waitingRows("Timers that have not fired", w.timers, i => `due ${waitingWhen(i.when)}`) +
    waitingRows("Calls that wait for your yes", w.calls, i => `asked ${waitingWhen(i.when)}`) +
    waitingRows("Messages that are queued or could not be sent", w.messages,
      i => `${esc(i.status)}, to ${esc(i.where)}`),
    {title: "Waiting"});
}

async function cancelWaiting(id){
  const r = await postJSON("/api/waiting/cancel", {id});
  waitingNote = r.error ? ("Not cancelled: " + r.error) : ("Cancelled " + id + ".");
  await refresh();
}
