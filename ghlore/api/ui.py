"""The web UI (section 8) -- built early, on purpose.

One self-contained page served by ``ghlored``, calling the *same* API the CLI calls. No
build step, no framework, no second ranking implementation: if the UI grows its own query
logic it stops being representative, and the thing you tuned is no longer the thing agents
use.

It is the instrument the rest of the work is calibrated with. After a backfill the only
question is "are these results any good?" -- no metric answers it, and a person reading ten
results answers it in a minute. Section 6's weights are a guess, and replacing them
honestly means running real queries and fixing the term that is wrong.

Three affordances exist for people and not for agents:

1. **Score breakdown per hit.** An agent has no use for *why* something ranked; the person
   tuning the weights has nothing else.
2. **View as the model sees it.** The ``rendered`` field from the API -- the exact string
   the CLI prints, envelope and delimiter scrubbing included -- so a formatting or
   injection bug is caught by a person reading it rather than by an agent meeting it
   mid-task. It is the server's own rendering, not a copy: :mod:`ghlore.render` has one
   implementation and two callers.
3. **Copy as CLI invocation.** Debugging a bad agent answer starts with reproducing it.

Plus the index-health strip, which beats a dashboard panel here because the question is
almost always "is the index current?" and the answer belongs next to the results.

Labelling writes section 10's evaluation set as a byproduct of use rather than as a chore
nobody schedules. It needs a token with the ``label`` scope and a configured path, and it
lands in a JSONL file that is never indexed -- see :mod:`ghlore.api.server`.
"""

from __future__ import annotations

_PAGE = """
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ghlore</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #fbfbfa; --fg: #1a1a1a; --dim: #6b6b6b; --line: #e0dfdc;
    --card: #ffffff; --accent: #1c5d99; --warn: #8a4b00; --mach: #8a1c1c;
    font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16181c; --fg:#e6e6e6; --dim:#9a9a9a; --line:#2c3038;
            --card:#1d2026; --accent:#7fb2e5; --warn:#e0a55c; --mach:#e08080; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg); }
  main { max-width: 60rem; margin: 0 auto; padding: 1.5rem 1rem 4rem; }
  h1 { font-size: 1.1rem; margin: 0 0 .25rem; letter-spacing: .02em; }
  h1 small { color: var(--dim); font-weight: 400; }
  form { display: flex; flex-wrap: wrap; gap: .5rem; margin: 1rem 0 .5rem; }
  input, select, button, textarea {
    font: inherit; color: inherit; background: var(--card);
    border: 1px solid var(--line); border-radius: 4px; padding: .4rem .55rem;
  }
  input[name=q] { flex: 1 1 22rem; }
  button { cursor: pointer; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .strip {
    display: flex; flex-wrap: wrap; gap: 0 1.25rem; padding: .5rem .75rem;
    border: 1px solid var(--line); border-radius: 4px; background: var(--card);
    color: var(--dim); font-size: .82rem;
  }
  .strip b { color: var(--fg); font-weight: 600; }
  .hit {
    border: 1px solid var(--line); border-radius: 4px; background: var(--card);
    padding: .7rem .85rem; margin: .6rem 0;
  }
  .hit h2 { font-size: .95rem; margin: 0 0 .2rem; }
  .hit h2 a { color: var(--accent); text-decoration: none; }
  .meta { color: var(--dim); font-size: .8rem; display: flex; flex-wrap: wrap; gap: .6rem; }
  .snippet { margin: .45rem 0 .3rem; white-space: pre-wrap; overflow-wrap: anywhere; }
  .tier { font-weight: 600; }
  .tier.authoritative { color: var(--accent); }
  .tier.reported { color: var(--warn); }
  .tier.machine { color: var(--mach); }
  .actions { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .4rem; }
  .actions button { font-size: .78rem; padding: .2rem .45rem; }
  .actions button.done { border-color: var(--accent); color: var(--accent); }
  details { margin-top: .4rem; }
  summary { cursor: pointer; color: var(--dim); font-size: .8rem; }
  table.terms { border-collapse: collapse; font-size: .8rem; margin-top: .3rem; }
  table.terms td { padding: .1rem .6rem .1rem 0; }
  pre {
    white-space: pre-wrap; overflow-wrap: anywhere; background: var(--card);
    border: 1px solid var(--line); border-radius: 4px; padding: .75rem;
    font: .8rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .note { color: var(--dim); font-size: .82rem; margin: .75rem 0; }
  .error { color: var(--mach); }
</style>

<main>
  <h1>ghlore <small id="backend">…</small></h1>
  <div class="strip" id="health">…</div>

  <form id="search">
    <input name="q" placeholder="error text, a symbol, or a question" autofocus>
    <select name="kind">
      <option value="">any kind</option>
      <option value="failure">failure</option>
      <option value="precedent">precedent</option>
      <option value="rationale">rationale</option>
    </select>
    <select name="trust">
      <option value="">human tiers</option>
      <option value="authoritative">authoritative only</option>
      <option value="machine">machine (our own bots)</option>
    </select>
    <input name="file" placeholder="path filter" size="16">
    <input name="labels" placeholder="labels, comma-separated" size="18">
    <button class="primary">search</button>
    <button type="button" id="toggle-raw">view as the model sees it</button>
  </form>
  <div class="note">
    <label>token <input id="token" size="24" placeholder="only if the daemon requires one"></label>
  </div>

  <div id="message" class="note"></div>
  <pre id="raw" hidden></pre>
  <div id="hits"></div>
</main>

<script>
const $ = (s) => document.querySelector(s);
const token = $("#token");
token.value = localStorage.getItem("ghlore-token") || "";
token.addEventListener("change", () => localStorage.setItem("ghlore-token", token.value));

const headers = () => {
  const h = {"content-type": "application/json"};
  if (token.value.trim()) h["authorization"] = "Bearer " + token.value.trim();
  return h;
};

let showRaw = false;
let last = null;

$("#toggle-raw").addEventListener("click", () => {
  showRaw = !showRaw;
  $("#toggle-raw").classList.toggle("done", showRaw);
  paint();
});

async function health() {
  try {
    const r = await fetch("/api/v1/status", {headers: headers()});
    if (!r.ok) throw new Error(await r.text());
    const s = await r.json();
    $("#backend").textContent =
      s.backend.name + " / " + s.backend.ranking + " · v" + s.version;
    const passes = (s.passes || []).map((p) =>
      `<span>${p.repo} <b>${p.pass}</b> high-water ${short(p.high_water)}` +
      ` · ok ${short(p.last_ok_at)}</span>`).join("");
    $("#health").innerHTML =
      `<span><b>${s.threads}</b> threads</span><span><b>${s.documents}</b> documents</span>` +
      `<span><b>${s.raw_objects}</b> staged</span>` + passes;
  } catch (e) {
    $("#health").innerHTML = `<span class="error">status unavailable: ${esc(e.message)}</span>`;
  }
}

$("#search").addEventListener("submit", async (event) => {
  event.preventDefault();
  const f = new FormData(event.target);
  const body = {
    query: f.get("q") || "",
    kind: f.get("kind") || null,
    trust: f.get("trust") || null,
    files: split(f.get("file")),
    labels: split(f.get("labels")),
    // Always asked for: the toggle switches what is displayed, not what the server did,
    // so "view as the model sees it" cannot show a different query's rendering.
    render: true,
  };
  $("#message").textContent = "searching…";
  try {
    const r = await fetch("/api/v1/search", {
      method: "POST", headers: headers(), body: JSON.stringify(body),
    });
    const payload = await r.json();
    if (!r.ok) throw new Error(JSON.stringify(payload.detail || payload));
    last = {payload, request: body};
    paint();
  } catch (e) {
    last = null;
    $("#hits").innerHTML = "";
    $("#message").innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
});

function paint() {
  $("#raw").hidden = !showRaw;
  if (!last) { $("#message").textContent = ""; return; }
  const {payload, request} = last;
  const floor = (payload.query.trust_floor || []).join(", ");
  $("#message").textContent =
    `${payload.count} hit${payload.count === 1 ? "" : "s"} · trust floor: ${floor}` +
    (payload.count === 0 ? " · nothing matched" : "");
  $("#raw").textContent = payload.rendered || "";
  $("#hits").innerHTML = showRaw ? "" : payload.hits.map((h, i) => card(h, i, request)).join("");
}

function card(h, i, request) {
  const terms = Object.entries(h.breakdown || {})
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v}</td></tr>`).join("");
  const cli = cliFor(h, request);
  return `<div class="hit" data-i="${i}">
    <h2><a href="${esc(h.url || "#")}" target="_blank" rel="noreferrer noopener">
      ${esc(h.repo)}#${h.number}</a> — ${esc(h.title || "")}</h2>
    <div class="meta">
      <span class="tier ${esc(h.trust)}">${esc(h.trust)}</span>
      <span>${esc(h.age)}</span><span>${esc(h.source_type)}</span>
      <span>${h.author ? "@" + esc(h.author) : ""}</span>
      <span>score ${h.score ?? "—"}</span>
    </div>
    <div class="snippet">${esc(h.snippet || "")}</div>
    <div class="actions">
      <button data-verdict="relevant" data-i="${i}">relevant</button>
      <button data-verdict="not_relevant" data-i="${i}">not relevant</button>
      <button data-verdict="decisive" data-i="${i}">decisive</button>
      <button data-copy="${esc(cli)}">copy as CLI</button>
    </div>
    <details><summary>score breakdown</summary>
      <table class="terms">${terms || "<tr><td>no terms yet</td></tr>"}</table>
    </details>
  </div>`;
}

function cliFor(h, request) {
  const parts = ["ghlore search"];
  if (request.query) parts.push(quote(request.query));
  if (request.kind) parts.push("--kind " + request.kind);
  if (request.trust) parts.push("--trust " + request.trust);
  (request.files || []).forEach((f) => parts.push("--file " + quote(f)));
  (request.labels || []).forEach((l) => parts.push("--label " + quote(l)));
  return parts.join(" ");
}

document.addEventListener("click", async (event) => {
  const copy = event.target.closest("button[data-copy]");
  if (copy) {
    await navigator.clipboard.writeText(copy.dataset.copy);
    copy.classList.add("done");
    return;
  }
  const vote = event.target.closest("button[data-verdict]");
  if (!vote || !last) return;
  const hit = last.payload.hits[Number(vote.dataset.i)];
  const r = await fetch("/api/v1/label", {
    method: "POST", headers: headers(),
    body: JSON.stringify({
      query: last.request.query, kind: last.request.kind, repo: hit.repo,
      number: hit.number, source_type: hit.source_type, verdict: vote.dataset.verdict,
      // The filters travel with the verdict: the same text under a different `--file` is
      // a different question, and section 10's set has to be able to tell them apart.
      filters: {
        files: last.request.files || [], symbols: last.request.symbols || [],
        errors: last.request.errors || [], tests: last.request.tests || [],
      },
    }),
  });
  if (r.ok) {
    vote.closest(".actions").querySelectorAll("[data-verdict]")
      .forEach((b) => b.classList.remove("done"));
    vote.classList.add("done");
  } else {
    const body = await r.json().catch(() => ({}));
    $("#message").innerHTML =
      `<span class="error">labelling: ${esc(body.detail || r.status)}</span>`;
  }
});

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const split = (s) => String(s || "").split(",").map((x) => x.trim()).filter(Boolean);
const quote = (s) => /[\\s"']/.test(s) ? "'" + String(s).replace(/'/g, "'\\\\''") + "'" : s;
const short = (iso) => iso ? String(iso).slice(0, 16).replace("T", " ") : "never";

health();
</script>
"""


def page() -> str:
    return _PAGE.strip()
