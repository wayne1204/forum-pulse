// Forum Pulse dashboard. Data files are JS (FP.loaded(key, data)) so the page
// works from file:// as well as from the local server.
const FP = (() => {
  const cache = {}, waiting = {};
  return {
    loaded(key, data) { cache[key] = data; (waiting[key] || []).forEach(f => f(data)); delete waiting[key]; },
    load(key, bust = "") {
      if (cache[key]) return Promise.resolve(cache[key]);
      return new Promise((resolve, reject) => {
        (waiting[key] = waiting[key] || []).push(resolve);
        const s = document.createElement("script");
        s.src = `data/${key}.js${bust ? "?v=" + encodeURIComponent(bust) : ""}`;
        s.onerror = () => { delete waiting[key]; reject(new Error("missing " + key)); };
        document.head.appendChild(s);
      });
    },
  };
})();

const $ = (sel, el = document) => el.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Sign from the rounded figure, so a tiny loss never reads "−0.0%".
const signed = (x, d) => { const r = Number(x.toFixed(d)); return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r).toFixed(d); };
const pct = (v, d = 1) => v == null ? "–" : signed(v * 100, d) + "%";
// Taiwan convention: red is up, green is down; from the rounded figure, like signed().
const updown = (v, d = 1) => { const r = v == null ? 0 : Number((v * 100).toFixed(d)); return r > 0 ? "up" : r < 0 ? "down" : ""; };
// A Stance Group as a pill: red Bullish, blue Bearish, grey Split.
const group = g => g ? `<span class="grp grp-${g.toLowerCase()}">${g}</span>` : `<span class="muted">–</span>`;
const sgn = (v, d = 2) => v == null ? "–" : signed(v, d);
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
let META = null, charts = [];

function stanceBar(t) {
  if (!t || !t.labelled) return `<span class="muted">not labelled</span>`;
  const w = k => (100 * t[k] / t.labelled).toFixed(1) + "%";
  return `<span class="stancebar" title="bullish ${t.bullish} · bearish ${t.bearish} · neutral ${t.neutral} · mixed ${t.mixed}">
    <span class="b" style="width:${w("bullish")}"></span><span class="m" style="width:${w("mixed")}"></span>
    <span class="u" style="width:${w("neutral")}"></span><span class="r" style="width:${w("bearish")}"></span></span>
    <span class="muted"> ${t.bullish}/${t.bearish}</span>`;
}
const legend = () => `<div class="legend"><span><i style="background:var(--bull)"></i>bullish 偏多</span>
  <span><i style="background:var(--mixed)"></i>mixed</span><span><i style="background:var(--neutral)"></i>neutral</span>
  <span><i style="background:var(--bear)"></i>bearish 偏空</span><span>· numbers: bullish/bearish Mentions</span></div>`;

function setNav(which) {
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("on", a.dataset.nav === which));
}

// ---- Day ------------------------------------------------------------------
async function dayView(day) {
  setNav("day");
  const days = META.days;
  if (!days.length) { $("#view").innerHTML = `<div class="empty">No data yet — run <code>./run.sh daily</code>.</div>`; return; }
  if (!day || !days.includes(day)) day = nearestDay(day) || days[days.length - 1];
  const i = days.indexOf(day);
  const d = await FP.load("day/" + day, META.built);
  const dow = DOW[new Date(day + "T00:00:00").getDay()];
  const m = d.market;
  $("#view").innerHTML = `
    <h1>What the forum talked about</h1>
    <p class="sub">PTT Stock · each user counts once per Instrument per Forum Day · Top ${META.config.top_n} and Buzz Spikes are Signals</p>
    <div class="daypick">
      <button id="prev" ${i <= 0 ? "disabled" : ""} title="previous day (←)">‹ Prev</button>
      <input type="date" id="pick" value="${day}" min="${days[0]}" max="${days[days.length - 1]}">
      <span class="dow">${dow}</span>
      <button id="next" ${i >= days.length - 1 ? "disabled" : ""} title="next day (→)">Next ›</button>
      <button id="latest" ${i === days.length - 1 ? "disabled" : ""}>Latest</button>
      <span class="muted">${days.length} days · built ${esc(META.built.replace("T", " "))}</span>
    </div>
    <div class="tiles">
      <div class="tile"><div class="k">Comments</div><div class="v">${d.comments.toLocaleString()}</div><div class="s">posts and pushes this day</div></div>
      <div class="tile"><div class="k">Mentions</div><div class="v">${d.mentions.toLocaleString()}</div><div class="s">user × Instrument</div></div>
      <div class="tile"><div class="k">Market Net Stance</div><div class="v">${m ? sgn(m.net) : "–"}</div>
        <div class="s">${m ? `${m.mentions} Mentions of 大盤/台指 · ${m.tally ? m.tally.bullish + " bull / " + m.tally.bearish + " bear" : "not labelled"}` : "no Market Mentions"}</div></div>
      <div class="tile"><div class="k">Review Queue</div><div class="v">${d.queued}</div><div class="s"><a href="review.html">ambiguous Aliases waiting</a></div></div>
    </div>
    ${legend()}
    <div class="card"><table>
      <thead><tr><th class="n">#</th><th>Instrument</th><th class="n">Mentions</th><th>Signal</th><th>Stance</th>
        <th class="n">Net</th><th>Group</th>${["1D", "1W", "1M", "3M"].map(h => `<th class="n" title="Return from Entry (next trading day's open)">${h}</th>`).join("")}</tr></thead>
      <tbody>${baselineRow(d.baseline)}${d.rows.map((r, k) => dayRow(r, k)).join("") || `<tr><td colspan="11" class="empty">No Mentions.</td></tr>`}</tbody>
    </table></div>
    <p class="note">1D–3M = the Instrument's own return from Entry (the next trading day's open; US and Korean stocks on their own trading days), dividends included. The first row is TAIEX total return over the same days, to compare against. Blank until enough days have passed.
      Click a row for its comments.</p>`;
  const go = dd => { location.hash = "#/day/" + dd; };
  $("#prev").onclick = () => go(days[i - 1]);
  $("#next").onclick = () => go(days[i + 1]);
  $("#latest").onclick = () => go(days[days.length - 1]);
  $("#pick").onchange = e => go(nearestDay(e.target.value) || day);
  document.querySelectorAll("tr.rowx").forEach(tr => tr.onclick = e => {
    if (e.target.closest("a")) return;
    const s = tr.nextElementSibling; s.hidden = !s.hidden;
    if (!s.hidden && !s.dataset.done) { s.dataset.done = "1"; showComments(s.firstElementChild, day, tr.dataset.code); }
  });
}

function nearestDay(want) {
  if (!want) return null;
  const days = META.days;
  if (days.includes(want)) return want;
  const before = days.filter(d => d <= want);
  return before.length ? before[before.length - 1] : days[0];
}

function dayRow(r, k) {
  const chips = (r.top ? `<span class="chip top">TOP</span>` : "") + (r.spike ? `<span class="chip spike">SPIKE</span>` : "");
  const f = r.ret || {};
  return `<tr class="rowx" data-code="${esc(r.code)}">
    <td class="n muted">${r.rank ?? ""}</td>
    <td><a href="#/inst/${encodeURIComponent(r.code)}">${esc(r.code)} ${esc(r.name)}</a></td>
    <td class="n">${r.mentions}</td><td>${chips}</td><td>${stanceBar(r.tally)}</td>
    <td class="n">${sgn(r.net)}</td><td>${group(r.group)}</td>
    ${["1D", "1W", "1M", "3M"].map(h => `<td class="n ${updown(f[h])}">${pct(f[h])}</td>`).join("")}
  </tr><tr class="samples" hidden><td colspan="11"></td></tr>`;
}

// TAIEX total return over the same Horizons: what any stock is up against.
function baselineRow(b) {
  if (!b) return "";
  return `<tr class="baseline"><td></td><td colspan="6">TAIEX total return <span class="muted">— baseline</span></td>
    ${["1D", "1W", "1M", "3M"].map(h => `<td class="n ${updown(b[h])}">${pct(b[h])}</td>`).join("")}</tr>`;
}

// Every comment naming the row's Instrument, filtered by its user's Stance.
const STANCES = [["all", "All"], ["bullish", "Bullish 偏多"], ["bearish", "Bearish 偏空"],
                 ["neutral", "Neutral"], ["mixed", "Mixed"], ["none", "Unlabelled"]];
async function showComments(td, day, code) {
  td.innerHTML = `<span class="muted">Loading comments…</span>`;
  let d;
  try { d = await FP.load("cmt/" + day, META.built); }
  catch { td.innerHTML = `<span class="muted">No comments file for this day; run <code>./run.sh rebuild</code>.</span>`; return; }
  const all = d.by_code[code] || [];
  const key = c => c[4] || "none";
  const n = {}; all.forEach(c => n[key(c)] = (n[key(c)] || 0) + 1);
  const line = ([user, tag, text, pid, stance]) => `<div class="c"><b>${esc(user)}</b> ${esc(tag)}: ${esc(text)}
      ${stance ? `<span class="chip st-${stance}">${stance}</span>` : ""}
      <a href="${esc(d.url + pid)}.html" target="_blank" rel="noopener" class="muted">${esc(d.posts[pid])}</a></div>`;
  const draw = want => {
    const shown = want === "all" ? all : all.filter(c => key(c) === want);
    td.innerHTML = `<div class="cfilter">${STANCES.filter(([k]) => k === "all" || n[k]).map(([k, label]) =>
        `<button data-k="${k}" class="${k === want ? "on" : ""}">${label} ${k === "all" ? all.length : n[k]}</button>`).join("")}</div>
      <div class="clist">${shown.map(line).join("") || `<span class="muted">No comments.</span>`}</div>`;
    td.querySelectorAll(".cfilter button").forEach(b => b.onclick = () => draw(b.dataset.k));
  };
  draw("all");
}

// ---- Instrument --------------------------------------------------------------
async function instView(code) {
  setNav("");
  let d;
  try { d = await FP.load("inst/" + code, META.built); }
  catch { $("#view").innerHTML = `<div class="empty">${esc(code)} has never been among a day's most mentioned.</div>`; return; }
  const sigs = d.signals.slice().sort((a, b) => b.day.localeCompare(a.day));
  $("#view").innerHTML = `
    <h1>${esc(d.code)} ${esc(d.name)}</h1>
    <p class="sub">Daily, over the same days. ${d.code === "MARKET" ? "Price is the TAIEX total-return index." : "Price is dividend-adjusted close."}</p>
    <div class="charts">
      <div class="chartbox"><h3>Price</h3><div class="c"><canvas id="c-price"></canvas></div></div>
      <div class="chartbox"><h3>Mentions <span class="muted">— outlined bars were Signals</span></h3><div class="c"><canvas id="c-ment"></canvas></div></div>
      <div class="chartbox"><h3>Net Stance <span class="muted">— +1 all bullish, −1 all bearish</span></h3><div class="c"><canvas id="c-net"></canvas></div></div>
    </div>
    <h2>Signals and what followed</h2>
    <div class="card"><table><thead><tr><th>Forum Day</th><th>Signal</th><th class="n">Net</th><th>Group</th>
      ${["1D", "1W", "1M", "3M"].map(h => `<th class="n">${h}</th>`).join("")}</tr></thead>
      <tbody>${sigs.map(s => `<tr><td><a href="#/day/${s.day}">${s.day}</a></td><td>${s.kind}</td><td class="n">${sgn(s.net_stance)}</td>
        <td>${group(s.group)}</td>${["1D", "1W", "1M", "3M"].map(h => `<td class="n ${updown(s[h])}">${pct(s[h])}</td>`).join("")}</tr>`).join("")
        || `<tr><td colspan="8" class="empty">No Signals with prices yet.</td></tr>`}</tbody></table></div>`;
  drawInst(d);
}

function drawInst(d) {
  charts.forEach(c => c.destroy()); charts = [];
  if (!window.Chart) return;
  const labels = d.series.map(r => r[0]);
  const grid = css("--grid"), ink = css("--text-secondary");
  Chart.defaults.color = ink; Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  const base = (yopts = {}) => ({
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false }, tooltip: { displayColors: false } },
    scales: { x: { offset: true, grid: { display: false }, ticks: { maxTicksLimit: 10, autoSkip: true } },
              y: { grid: { color: grid }, border: { display: false }, ...yopts } },
    onClick: (e, els) => { if (els.length) location.hash = "#/day/" + labels[els[0].index]; },
  });
  charts.push(new Chart($("#c-price"), { type: "line", data: { labels, datasets: [{
    data: d.series.map(r => r[3]), borderColor: css("--accent"), borderWidth: 2, pointRadius: 0, spanGaps: true }] },
    options: base() }));
  const accent = css("--accent"), mixed = css("--mixed"), neutral = css("--neutral");
  charts.push(new Chart($("#c-ment"), { type: "bar", data: { labels, datasets: [{
    data: d.series.map(r => r[1]),
    backgroundColor: d.series.map(r => r[4] || r[5] ? accent : neutral),
    borderColor: d.series.map(r => r[5] ? mixed : "transparent"), borderWidth: d.series.map(r => r[5] ? 2 : 0),
    borderRadius: 2 }] }, options: base({ beginAtZero: true }) }));
  const bull = css("--bull"), bear = css("--bear");
  charts.push(new Chart($("#c-net"), { type: "bar", data: { labels, datasets: [{
    data: d.series.map(r => r[2]), backgroundColor: d.series.map(r => r[2] == null ? neutral : r[2] >= 0 ? bull : bear),
    borderRadius: 2 }] }, options: base({ min: -1, max: 1 }) }));
}

// ---- Backtest --------------------------------------------------------------------
async function backtestView() {
  setNav("backtest");
  const b = await FP.load("backtest", META.built);
  const H = b.horizons, cfg = META.config;
  const cell = s => s && s.n ? `<td class="n cell"><div class="main ${updown(s.mean, 2)}">${pct(s.mean, 2)}</div>
      <div class="sm">med ${pct(s.median, 2)} · beat ${(s.hit * 100).toFixed(0)}% · n ${s.n}</div></td>` : `<td class="n cell muted">–</td>`;
  const table = kind => {
    const rows = ["All", "Bullish", "Split", "Bearish"].map(g => {
      const by = Object.fromEntries(b.summary.filter(s => s.kind === kind && s.group === g).map(s => [s.horizon, s]));
      return `<tr><td>${g === "All" ? g : group(g)}</td>${H.map(h => cell(by[h])).join("")}</tr>`;
    }).join("");
    return `<div class="card btgrid"><table><thead><tr><th>Stance Group</th>${H.map(h => `<th class="n">${h}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>`;
  };
  const mc = b.model_check;
  const recent = b.signals.slice().sort((x, y) => y.day.localeCompare(x.day)).slice(0, 300);
  $("#view").innerHTML = `
    <h1>Backtest</h1>
    <p class="sub">Mean Excess Return from Entry (next trading day's open) vs TAIEX total return (overseas stocks: their own market's index), by Stance Group.
      Bullish ≥ +${cfg.cutoff}, Bearish ≤ −${cfg.cutoff}, needs ≥ ${cfg.min_decided} bullish-or-bearish Mentions.</p>
    <h2>Top Mentioned <span class="muted">— the ${cfg.top_n} most-mentioned Instruments each day</span></h2>${table("Top Mentioned")}
    <h2>Buzz Spike <span class="muted">— ≥ ${cfg.spike_ratio}× its 20-trading-day average and ≥ ${cfg.spike_min} Mentions</span></h2>${table("Buzz Spike")}
    <h2>Market <span class="muted">— raw TAIEX total return after days the forum talked about 大盤/台指</span></h2>${table("Market")}
    <p class="note">Model check: on ${mc.n} 標的 posts whose author declared 多/空, the model agreed ${mc.n ? (100 * mc.agree / mc.n).toFixed(0) + "%" : "–"} of the time.
      Overlapping Horizons make Signals far from independent — treat n as an upper bound on evidence.</p>
    <h2>Recent Signals</h2>
    <div class="card"><table><thead><tr><th>Forum Day</th><th>Instrument</th><th>Signal</th><th class="n">Net</th><th>Group</th>
      ${H.map(h => `<th class="n">${h}</th>`).join("")}</tr></thead><tbody>
      ${recent.map(s => `<tr><td><a href="#/day/${s.day}">${s.day}</a></td><td><a href="#/inst/${encodeURIComponent(s.code)}">${esc(s.code)} ${esc(s.name)}</a></td>
        <td>${s.kind}</td><td class="n">${sgn(s.net_stance)}</td><td>${group(s.group)}</td>${H.map(h => `<td class="n ${updown(s[h])}">${pct(s[h])}</td>`).join("")}</tr>`).join("")
        || `<tr><td colspan="9" class="empty">No Signals with prices yet.</td></tr>`}
    </tbody></table></div>`;
}

// ---- routing -----------------------------------------------------------------------
async function route() {
  const [, page, arg] = location.hash.split("/");
  window.scrollTo(0, 0);
  try {
    if (page === "inst" && arg) await instView(decodeURIComponent(arg));
    else if (page === "backtest") await backtestView();
    else await dayView(arg);
  } catch (e) {
    $("#view").innerHTML = `<div class="empty">Could not load: ${esc(e.message)}</div>`;
  }
}

document.addEventListener("keydown", e => {
  if (e.target.matches("input, select, textarea")) return;
  if (!location.hash.startsWith("#/day") && location.hash) return;
  if (e.key === "ArrowLeft") $("#prev")?.click();
  if (e.key === "ArrowRight") $("#next")?.click();
});

FP.load("meta", String(Date.now())).then(meta => {
  META = meta;
  $("#inst-list").innerHTML = meta.instruments.map(([c, n]) => `<option value="${esc(c)} ${esc(n)}">`).join("");
  $("#find").onchange = e => {
    const v = e.target.value.trim(), code = v.split(/\s+/)[0];
    const hit = meta.instruments.find(([c, n]) => c === code || n === v || v.includes(n));
    if (hit) { location.hash = "#/inst/" + hit[0]; e.target.value = ""; }
  };
  window.addEventListener("hashchange", route);
  route();
}).catch(() => { $("#view").innerHTML = `<div class="empty">No data yet — run <code>./run.sh daily</code>.</div>`; });
