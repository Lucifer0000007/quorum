"use strict";

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ACTIVE = new Set(["provisional", "implementable", "implemented"]);
const VERDICT_TEXT = {
  CLEAN: "No active KEP depends on it within three hops.",
  AT_RISK: "Some active work or a stale dependency is in the path.",
  HUMAN_GATE_REQUIRED: "Several SIGs are affected. A human has to approve the notice.",
};
const EXAMPLES = [
  "Which SIG owns KEP-2221?",
  "Does KEP-2377 have a successor?",
  "Who knows about sig-scheduling?",
  "Which KEPs list KEP-1287 in see-also?",
  "Why does KEP-4381 exist?",
];
let health = null;

function sessionId() {
  try {
    let id = localStorage.getItem("quorum-session");
    if (!id) { id = "s-" + Math.random().toString(36).slice(2, 10); localStorage.setItem("quorum-session", id); }
    return id;
  } catch { return "s-ephemeral"; }
}

async function call(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({ error: "bad_response", detail: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

function showError(msg) {
  const el = $("error");
  el.hidden = !msg;
  el.textContent = msg ? `Couldn't complete that: ${msg}` : "";
}

function busy(form, on) { form.querySelectorAll("button").forEach((b) => (b.disabled = on)); }

async function loadHealth() {
  try {
    health = await fetch("/health").then((r) => r.json());
  } catch (e) { health = { graph_ready: false, detail: String(e) }; }
  const g = $("st-graph");
  if (health.graph_ready) {
    g.innerHTML = `<span class="ok">ready</span>, data ${esc(health.meta.data_hash.slice(0, 10))}`;
    const c = health.counts;
    $("st-counts").textContent = `${c["node:KEP"]} KEPs, ${c["node:Person"]} people, ${c["node:SIG"]} SIGs`;
  } else {
    g.innerHTML = `<span class="bad">not ready</span>`;
    showError(health.detail || "FalkorDB graph is not ready. Run make up and make ingest.");
  }
  const llm = health.llm || {};
  $("st-llm").innerHTML = llm.configured ? `language model (${esc(llm.model)})` : `keywords <span class="bad">(no API key set)</span>`;
  const ev = health.eval;
  $("st-eval").innerHTML = ev
    ? `${ev.passed}/${ev.total} correct (${(ev.rate * 100).toFixed(1)}%)${ev.stale ? ' <span class="bad">stale</span>' : ""}`
    : "not run yet";
}

function renderQueries(queries) {
  if (!queries || !queries.length) return;
  $("cypher").innerHTML = queries.map((q) => `
    <article class="query">
      <header><b>${esc(q.name)}</b><span>${esc(q.graph)}, ${q.rows ?? "–"} rows, ${q.ms ?? "–"} ms</span></header>
      <pre>${esc(q.cypher)}</pre>
      <pre class="params">${esc(JSON.stringify(q.params))}</pre>
    </article>`).join("");
}

function pathText(edges) {
  return (edges || []).map(([a, rel, b]) => `${a} -[${rel}]-> ${b}`).join("   ");
}

function sources(links) {
  if (!links || !links.length) return "";
  return `<h3>Sources</h3><ul class="sources">${links.map((l) => `<li><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.label)}</a></li>`).join("")}</ul>`;
}

function statusPill(s) {
  const cls = ACTIVE.has(s) ? "active" : ["replaced", "rejected", "withdrawn", "deferred"].includes(s) ? "stale" : "";
  return `<span class="status-pill ${cls}">${esc(s)}</span>`;
}

function kepTable(rows, withPath) {
  if (!rows.length) return "";
  return `<table><thead><tr><th>KEP</th><th>Title</th><th>Status</th><th>Owning SIG</th>${withPath ? "<th>Hops</th>" : ""}</tr></thead><tbody>
    ${rows.map((r) => `<tr>
      <td><a href="${esc(r.source_url)}" target="_blank" rel="noopener">KEP-${esc(r.id)}</a></td>
      <td>${esc(r.title)}${withPath ? `<div class="path">${esc(pathText(r.path_edges))}</div>` : ""}</td>
      <td>${statusPill(r.status)}</td><td>${esc(r.owning_sig)}</td>
      ${withPath ? `<td class="num">${r.hops}</td>` : ""}</tr>`).join("")}
  </tbody></table>`;
}

function peopleTable(people) {
  return `<table><thead><tr><th>Person</th><th>Score</th><th>KEPs</th><th>Recent</th><th>Roles</th></tr></thead><tbody>
    ${people.map((p) => `<tr><td><a href="https://github.com/${esc(p.handle)}" target="_blank" rel="noopener">@${esc(p.handle)}</a></td>
      <td class="num">${p.score}</td><td class="num">${p.keps}</td><td class="num">${p.recent_contributions}</td>
      <td>${esc((p.roles || []).join(", ").toLowerCase())}</td></tr>`).join("")}
  </tbody></table>`;
}

function toolDetail(out) {
  const r = out.result;
  switch (out.tool) {
    case "blast_radius": return kepTable(r.reachable, true);
    case "referrers": return kepTable(r.referrers, false);
    case "who_knows_kep":
    case "who_knows_sig": return `${peopleTable(r.people)}<p class="meta">${esc(r.formula)}</p>`;
    case "is_still_valid":
      return (r.successors.length ? `<h3>Successors</h3>${r.successors.map((s) => `<p>KEP-${esc(s.id)} ${esc(s.title)} (${esc(s.status)})</p>`).join("")}` : "")
        + (r.stale_ancestors.length ? `<h3>Depends on stale KEPs</h3>${r.stale_ancestors.map((s) => `<p>KEP-${esc(s.id)} ${esc(s.title)} (${esc(s.status)}) <span class="path">${esc(s.path.join(" → "))}</span></p>`).join("")}` : "");
    case "why":
      return `<table><tbody>${r.chain.map((c) => `<tr><td>${esc(c.step)}</td><td>${c.source ? `<a href="${esc(c.source)}" target="_blank" rel="noopener">${esc(c.value)}</a>` : esc(c.value)}</td></tr>`).join("")}</tbody></table><p class="meta">${esc(r.note)}</p>`;
    case "kep_detail": {
      const p = r.people;
      return `<table><tbody>
        <tr><td>Participating SIGs</td><td>${esc(r.participating_sigs.join(", ") || "none listed")}</td></tr>
        <tr><td>Authors</td><td>${esc(p.AUTHORED.map((h) => "@" + h).join(", "))}</td></tr>
        <tr><td>Reviewers</td><td>${esc(p.REVIEWED.map((h) => "@" + h).join(", ") || "none listed")}</td></tr>
        <tr><td>Approvers</td><td>${esc(p.APPROVED.map((h) => "@" + h).join(", ") || "none listed")}</td></tr>
        <tr><td>Linked KEPs</td><td>${r.neighbors.map((n) => `${n.direction === "out" ? "→" : "←"} ${esc(n.rel)} KEP-${esc(n.id)}`).join("<br>") || "none"}</td></tr>
      </tbody></table>`;
    }
    default: return "";
  }
}

function renderAnswer(out) {
  if (out.tool === "preflight") return renderPreflight(out);
  $("output").innerHTML = `
    <section class="panel">
      <p class="answer">${esc(out.summary)}</p>
      <p class="meta">Tool ${esc(out.tool)} with ${esc(JSON.stringify(out.tool_params))}, routed by ${esc(out.router)}.</p>
      ${toolDetail(out)}
      ${out.path && out.path.length ? `<h3>Graph path</h3><p class="path">${esc(pathText(out.path.slice(0, 40)))}</p>` : ""}
      ${sources(out.source_links)}
    </section>`;
  renderQueries(out.queries);
}

function renderPreflight(out) {
  const n = out.staged_notice;
  const prior = out.prior_precedents || [];
  const reviewers = out.suggested_reviewers || [];
  $("output").innerHTML = `
    ${prior.length ? `<div class="precedent"><b>This has been checked before.</b> ${prior.length} precedent(s) on record; latest: ${esc(prior[0].verdict)} approved ${esc(prior[0].created_at)} (session ${esc(prior[0].session_id)}).</div>` : ""}
    <div class="verdict ${esc(out.verdict)} reveal">
      <div class="word">${esc(out.verdict.replaceAll("_", " ").toLowerCase())}</div>
      <p class="what">Superseding KEP-${esc(out.kep.id)}: ${esc(out.kep.title)}</p>
      <p class="why">${esc(VERDICT_TEXT[out.verdict])}</p>
    </div>
    <section class="panel">
      <h2>Why</h2>
      <ul class="reasons">${out.reasons.map((r) => `<li><span class="rule">${esc(r.rule)}</span>${esc(r.detail)}${r.evidence.length ? `<div class="meta">${esc(r.evidence.slice(0, 12).join(", "))}</div>` : ""}</li>`).join("")}</ul>
      <p class="meta">Rules: gate at ${out.rules.min_sigs_for_gate}+ SIGs, ${out.rules.hops} hops, recent means latest milestone ≥ v1.${out.rules.recent_min_minor}.</p>
    </section>
    <section class="panel">
      <h2>Blast radius</h2>
      ${out.no_path_found ? `<p class="answer">No path found. KEP-${esc(out.kep.id)} has no REPLACES or SEE_ALSO link within three hops.</p>`
        : `<p class="meta">${out.affected_keps.length} active KEP(s) in ${out.affected_sigs.length} SIG(s): ${esc(out.affected_sigs.join(", "))}. ${out.inactive_reachable.length} inactive KEP(s) also reachable.</p>${kepTable(out.affected_keps, true)}`}
    </section>
    ${reviewers.length ? `<section class="panel"><h2>Suggested reviewers</h2>${peopleTable(reviewers.slice(0, 6))}<p class="meta">${esc(out.who_knows_formula)}</p></section>` : ""}
    <section class="panel">
      <h2>Staged impact notice</h2>
      <div class="notice" id="notice">${esc(n.markdown)}</div>
      <div id="llm-summary"></div>
      <div class="gate">
        <button class="primary" id="approve">Approve and record precedent</button>
        ${health?.llm?.configured ? `<button id="summarize">Summarize with the language model</button>` : ""}
        <p class="hint">Approving writes a Precedent to the agent_episodic_memory graph. Nothing is posted to GitHub or anywhere else.</p>
      </div>
      <p id="approved" aria-live="polite"></p>
    </section>
    <section class="panel">${sources(out.source_links)}</section>`;
  renderQueries(out.queries);
  $("approve").onclick = async (ev) => {
    ev.target.disabled = true;
    try {
      const a = await call("/approve", { notice_id: n.id, session_id: sessionId() });
      $("approved").innerHTML = `<span class="recorded">Precedent recorded</span> in ${esc(a.graph)} (id ${esc(a.precedent.id)}, ${esc(a.precedent.created_at)}). ${a.prior_precedents.length} precedent(s) now on record for KEP-${esc(n.kep_id)}. Posted externally: no.`;
      renderQueries([{ name: "record_precedent (approval gate write)", graph: a.graph, cypher: a.cypher, params: a.params, rows: 1, ms: "–" }]);
    } catch (e) { showError(e.message); ev.target.disabled = false; }
  };
  const s = $("summarize");
  if (s) s.onclick = async () => {
    s.disabled = true;
    try {
      const r = await call("/notice/summarize", { notice_id: n.id });
      $("llm-summary").innerHTML = `<p class="meta">${esc(r.note)}</p><p>${esc(r.summary)}</p>`;
    } catch (e) { showError(e.message); }
    s.disabled = false;
  };
}

$("ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("ask").value.trim();
  if (!q) return;
  showError(""); busy(ev.target, true);
  try { renderAnswer(await call("/ask", { question: q, router: "auto" })); } catch (e) { showError(e.message); }
  busy(ev.target, false);
});

$("kep-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const raw = $("kep").value.trim();
  if (!raw) return;
  const id = raw.split(/\s/)[0].replace(/^KEP-/i, "");
  showError(""); busy(ev.target, true);
  try { renderPreflight(await call("/preflight", { kep_id: id })); } catch (e) { showError(e.message); }
  busy(ev.target, false);
});

let searchTimer;
$("kep").addEventListener("input", (ev) => {
  clearTimeout(searchTimer);
  const q = ev.target.value.trim();
  if (q.length < 2) return;
  searchTimer = setTimeout(async () => {
    try {
      const r = await call(`/keps?q=${encodeURIComponent(q)}&limit=15`);
      $("kep-options").innerHTML = r.result.map((k) => `<option value="${esc(k.id)} ${esc(k.title)}"></option>`).join("");
    } catch { /* the picker is a convenience; errors show on submit */ }
  }, 200);
});

$("examples").innerHTML = "Try: " + EXAMPLES.map((e) => `<button type="button">${esc(e)}</button>`).join("");
$("examples").addEventListener("click", (ev) => {
  if (ev.target.tagName !== "BUTTON") return;
  $("ask").value = ev.target.textContent;
  $("ask-form").requestSubmit();
});

loadHealth();
