const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("on"), 2200);
}

async function api(path, body) {
  const r = await fetch("/api/" + path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

const label = c => c.code === "NOT" ? "Not an Instrument" : `${c.code} ${c.name}`;
const highlight = (text, alias) => esc(text).split(esc(alias)).join(`<mark>${esc(alias)}</mark>`);

async function load() {
  let q;
  try { q = await api("queue"); }
  catch (e) {
    $("#queue").innerHTML = `<div class="empty">The Review Queue needs the local server: run <code>./run.sh serve</code> and open http://127.0.0.1:8765/review.html</div>`;
    return;
  }
  $("#queue").innerHTML = q.length ? q.map(a => `
    <div class="alias" data-alias="${esc(a.alias)}">
      <h3>${esc(a.alias)} <span class="muted">· ${a.count} Comments waiting</span></h3>
      <div class="row"><span class="muted">Always means:</span>
        ${a.candidates.map(c => `<button data-act="always" data-code="${esc(c.code)}">${esc(label(c))}</button>`).join("")}
      </div>
      <div class="row"><span class="muted">When the Comment contains</span>
        <input class="ctx" placeholder="e.g. 航空" size="8">
        <select class="ctxcode">${a.candidates.map(c => `<option value="${esc(c.code)}">${esc(label(c))}</option>`).join("")}</select>
        <button data-act="context">Add</button>
      </div>
      ${a.comments.map(c => `<div class="cm" data-id="${c.id}">
        <span class="muted">${c.day}</span><b>${esc(c.user)}</b>
        <span class="t">${highlight(c.text, a.alias)} <a class="muted" href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title)}</a></span>
        <select class="one">${a.candidates.map(x => `<option value="${esc(x.code)}">${esc(label(x))}</option>`).join("")}</select>
        <button data-act="comment">This one</button>
      </div>`).join("")}
    </div>`).join("") : `<div class="empty">Nothing waiting. 🎉</div>`;
  const rules = await api("rules");
  $("#rules").innerHTML = rules.map(r => `<tr><td>${esc(r.alias)}</td>
    <td>${r.scope === "context" ? "contains “" + esc(r.context) + "”" : r.scope === "comment" ? "Comment #" + r.comment_id : "always"}</td>
    <td>${esc(r.code === "NOT" ? "Not an Instrument" : r.code + " " + (r.name || ""))}</td>
    <td><button data-del="${r.id}">Remove</button></td></tr>`).join("") || `<tr><td colspan="4" class="muted">No rules yet.</td></tr>`;
}

document.addEventListener("click", async e => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.del) { await api("rule/delete", { id: +b.dataset.del }); toast("Rule removed"); return load(); }
    const box = b.closest(".alias[data-alias]");
    if (!box || !b.dataset.act) return;
    const alias = box.dataset.alias, act = b.dataset.act;
    let rule = { alias, scope: act };
    if (act === "always") rule.code = b.dataset.code;
    if (act === "context") {
      rule.context = $(".ctx", box).value.trim(); rule.code = $(".ctxcode", box).value;
      if (!rule.context) return toast("Type a context word first");
    }
    if (act === "comment") { const cm = b.closest(".cm"); rule.comment_id = +cm.dataset.id; rule.code = $(".one", cm).value; }
    const res = await api("rule", rule);
    toast(`Saved · ${res.resolved} Comments resolved`);
    load();
  } catch (err) { toast("Error: " + err.message); }
});

$("#r-scope").onchange = e => { $("#r-context").hidden = e.target.value !== "context"; };
$("#r-add").onclick = async () => {
  const rule = { alias: $("#r-alias").value.trim(), scope: $("#r-scope").value,
                 context: $("#r-context").value.trim(), code: $("#r-code").value.trim().toUpperCase() };
  if (!rule.alias || !rule.code) return toast("Alias and code are both needed");
  try { const res = await api("rule", rule); toast(`Saved · ${res.resolved} Comments affected`); load(); }
  catch (err) { toast("Error: " + err.message); }
};

$("#rebuild").onclick = async () => {
  try { await api("rebuild", {}); poll(); } catch (err) { toast("Error: " + err.message); }
};
async function poll() {
  const s = await api("status");
  $("#status").textContent = s.running ? "Rebuilding…" : (s.last ? "Last rebuild: " + s.last : "");
  $("#rebuild").disabled = s.running;
  if (s.running) setTimeout(poll, 1500);
}
load(); poll().catch(() => {});
