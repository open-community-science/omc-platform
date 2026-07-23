"""Generate the interactive claim-provenance Artifact from the claims ledger.

Reads writings/claims_ledger.json (+ resolves data-path antecedents to their
values) and emits a self-contained HTML viewer: click a claim to see its verdict
and provenance — for computed claims, the actual code that produced it.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))
from results_explorer import _navigate  # noqa: E402  (resolves data paths)

W = HERE / "writings"
ledger = json.loads((W / "claims_ledger.json").read_text())
claims, comps = ledger["claims"], ledger["computations"]

# Resolve each antecedent to a displayable node for the viewer.
def resolve(ant):
    if ant in comps:
        c = comps[ant]
        return {"id": ant, "type": "computation", "label": c["label"],
                "code": c["code"], "result": json.dumps(c["result"])[:1200]}
    found, val = _navigate(ant)
    return {"id": ant, "type": "data", "label": ant,
            "value": (json.dumps(val)[:300] if found else "—"), "found": found}

view = []
for c in claims:
    view.append({
        "id": c["id"], "statement": c["statement"], "value": c["value"],
        "verdict": c.get("verdict", "unverifiable"), "kind": c.get("kind", "observation"),
        "method": c.get("method"),
        "antecedents": [resolve(a) for a in c.get("antecedents", [])],
    })

stats = {
    "claims": len(claims),
    "verified": sum(c.get("verdict") == "verified" for c in claims),
    "refuted": sum(c.get("verdict") == "refuted" for c in claims),
    "computations": len(comps),
}
study = "16S amplicon · frost flower · Ice Chamber Experiment (PRJNA1473294)"
DATA = json.dumps({"claims": view, "stats": stats, "study": study})

HTML = r'''<title>Claim Provenance — Results Autoresearch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#f4f7f6; --panel:#ffffff; --panel2:#eef2f1; --border:#d8e0df;
  --text:#13211f; --muted:#5a6a68; --accent:#0f8a80;
  --verified:#1f7a3d; --refuted:#c0392b; --unverifiable:#b07d15;
  --computation:#2266b8; --data:#5a6f6c;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0d1413; --panel:#131c1b; --panel2:#0f1716; --border:#243130;
  --text:#d7e1df; --muted:#8598956e; --muted:#899997; --accent:#34d3c3;
  --verified:#49c96e; --refuted:#f0685c; --unverifiable:#e2b53f;
  --computation:#5aa2ee; --data:#9fb0ad;
}}
:root[data-theme="light"]{
  --bg:#f4f7f6; --panel:#ffffff; --panel2:#eef2f1; --border:#d8e0df;
  --text:#13211f; --muted:#5a6a68; --accent:#0f8a80;
  --verified:#1f7a3d; --refuted:#c0392b; --unverifiable:#b07d15; --computation:#2266b8; --data:#5a6f6c;
}
:root[data-theme="dark"]{
  --bg:#0d1413; --panel:#131c1b; --panel2:#0f1716; --border:#243130;
  --text:#d7e1df; --muted:#899997; --accent:#34d3c3;
  --verified:#49c96e; --refuted:#f0685c; --unverifiable:#e2b53f; --computation:#5aa2ee; --data:#9fb0ad;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);
  line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 60px}
header{border-bottom:1px solid var(--border);padding-bottom:18px;margin-bottom:22px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--accent);margin:0 0 8px}
h1{font-size:clamp(22px,3.4vw,30px);margin:0 0 6px;letter-spacing:-.02em;text-wrap:balance;font-weight:650}
.sub{color:var(--muted);font-size:14px;margin:0}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:10px;
  padding:10px 14px;min-width:96px}
.stat .n{font-family:var(--mono);font-size:22px;font-variant-numeric:tabular-nums;font-weight:600}
.stat .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.stat.ok .n{color:var(--verified)} .stat.no .n{color:var(--refuted)}
.lede{background:var(--panel);border:1px solid var(--border);border-left:3px solid var(--accent);
  border-radius:10px;padding:13px 16px;margin:20px 0 6px;font-size:14px;color:var(--text)}
.lede b{color:var(--accent)}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:18px;margin-top:20px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
.col-h{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 10px}
ul.claims{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:7px;
  max-height:64vh;overflow:auto}
button.claim{width:100%;text-align:left;background:var(--panel);border:1px solid var(--border);
  border-radius:9px;padding:10px 12px;cursor:pointer;color:var(--text);font:inherit;
  display:flex;gap:10px;align-items:flex-start;transition:border-color .12s,background .12s}
button.claim:hover{border-color:var(--accent)}
button.claim[aria-selected="true"]{border-color:var(--accent);background:var(--panel2);
  box-shadow:inset 3px 0 0 var(--accent)}
button.claim:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.dot{width:9px;height:9px;border-radius:50%;margin-top:6px;flex:0 0 auto}
.dot.verified{background:var(--verified)} .dot.refuted{background:var(--refuted)}
.dot.unverifiable{background:var(--unverifiable)}
.claim .txt{font-size:13.5px;min-width:0}
.claim .cid{font-family:var(--mono);font-size:11px;color:var(--muted)}
.kind{display:inline-block;font-family:var(--mono);font-size:10px;letter-spacing:.04em;
  padding:1px 6px;border-radius:5px;border:1px solid var(--border);color:var(--muted);margin-left:6px}
.kind.caveat{color:var(--unverifiable);border-color:var(--unverifiable)}
.detail{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px;
  position:sticky;top:16px;max-height:78vh;overflow:auto}
.badge{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px;
  letter-spacing:.05em;text-transform:uppercase;padding:3px 9px;border-radius:20px;font-weight:600}
.badge.verified{background:color-mix(in srgb,var(--verified) 16%,transparent);color:var(--verified)}
.badge.refuted{background:color-mix(in srgb,var(--refuted) 16%,transparent);color:var(--refuted)}
.badge.unverifiable{background:color-mix(in srgb,var(--unverifiable) 16%,transparent);color:var(--unverifiable)}
.detail h2{font-size:17px;margin:12px 0 8px;line-height:1.35;text-wrap:balance;font-weight:600}
.kv{font-family:var(--mono);font-size:13px;background:var(--panel2);border:1px solid var(--border);
  border-radius:7px;padding:8px 11px;margin:8px 0;display:flex;gap:8px}
.kv .k{color:var(--muted)} .kv .v{font-variant-numeric:tabular-nums;word-break:break-word}
.prov-h{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  margin:20px 0 8px;border-top:1px solid var(--border);padding-top:14px}
.ant{border:1px solid var(--border);border-radius:9px;padding:10px 12px;margin:8px 0;background:var(--panel2)}
.ant .type{font-family:var(--mono);font-size:10px;letter-spacing:.05em;text-transform:uppercase;
  padding:1px 6px;border-radius:5px;color:#fff}
.ant .type.computation{background:var(--computation)} .ant .type.data{background:var(--data)}
.ant .lab{font-family:var(--mono);font-size:12.5px;margin-left:8px;color:var(--text)}
pre{margin:9px 0 0;background:var(--bg);border:1px solid var(--border);border-radius:7px;
  padding:10px 12px;overflow-x:auto;font-family:var(--mono);font-size:12px;line-height:1.45;color:var(--text)}
.ant .val{font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:6px;
  word-break:break-word;font-variant-numeric:tabular-nums}
.reverify{font-size:12px;color:var(--muted);margin-top:6px}
.reverify b{color:var(--verified)}
.foot{margin-top:34px;padding-top:16px;border-top:1px solid var(--border);color:var(--muted);font-size:12.5px}
.foot code{font-family:var(--mono);background:var(--panel2);padding:1px 5px;border-radius:4px}
.empty{color:var(--muted);font-size:13px;padding:20px 4px}
</style>

<div class="wrap">
  <header>
    <p class="eyebrow">Autoresearch · claim ledger</p>
    <h1>Every number traces to data or the code that produced it</h1>
    <p class="sub" id="study"></p>
    <div class="stats" id="stats"></div>
  </header>

  <div class="lede">
    Claims are the currency. Each was recorded by an agent exploring the pipeline data, and
    each carries a machine-checkable provenance — a <b>data path</b> or a re-runnable
    <b>computation</b>. An independent pass re-derived every one; only <b>verified</b> claims
    reach the manuscript. Select a claim to see what backs it.
  </div>

  <div class="grid">
    <section>
      <p class="col-h">Claims</p>
      <ul class="claims" id="list"></ul>
    </section>
    <aside>
      <p class="col-h">Provenance</p>
      <div class="detail" id="detail"><p class="empty">Select a claim to inspect its provenance.</p></div>
    </aside>
  </div>

  <p class="foot">Data path claims re-read the pipeline output; computation claims re-execute
  the stored code. The ledger is self-contained — any model or reviewer can re-run
  <code>verify()</code> and check every claim for themselves.</p>
</div>

<script>
const DATA = __DATA__;
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
document.getElementById('study').textContent = DATA.study;

const S = DATA.stats;
document.getElementById('stats').innerHTML = [
  ['claims', S.claims, ''], ['verified', S.verified, 'ok'],
  ['refuted', S.refuted, 'no'], ['computations', S.computations, '']
].map(([l,n,c]) => `<div class="stat ${c}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');

const list = document.getElementById('list');
DATA.claims.forEach((c,i) => {
  const b = document.createElement('button');
  b.className = 'claim'; b.setAttribute('role','option'); b.dataset.i = i;
  b.setAttribute('aria-selected','false');
  b.innerHTML = `<span class="dot ${c.verdict}"></span><span class="txt">`
    + `<span class="cid">${c.id}</span>`
    + (c.kind==='quality_caveat' ? '<span class="kind caveat">caveat</span>' : '')
    + `<br>${esc(c.statement)}</span>`;
  b.onclick = () => select(i);
  list.appendChild(b);
});

function select(i){
  [...list.children].forEach((el,j)=>el.setAttribute('aria-selected', j===i));
  const c = DATA.claims[i];
  const ants = c.antecedents.map(a => {
    if(a.type==='computation'){
      return `<div class="ant"><span class="type computation">computation</span>`
        + `<span class="lab">${a.id} · ${esc(a.label)}</span>`
        + `<pre>${esc(a.code)}</pre>`
        + `<div class="val">→ ${esc(a.result)}</div></div>`;
    }
    return `<div class="ant"><span class="type data">data</span>`
      + `<span class="lab">${esc(a.label)}</span>`
      + `<div class="val">= ${esc(a.value)}</div></div>`;
  }).join('') || '<p class="empty">No antecedents recorded.</p>';
  const how = {direct:'a direct data/computation read', 'x100':'a unit conversion (fraction→%)',
    '/100':'a unit conversion (%→fraction)', 'derived':'a derivation from its inputs'};
  const method = (c.method||'').split(',').map(m=>m.split(':')[0]).find(m=>m&&m!=='direct');
  const via = method ? ` via ${how[method]||method}` : '';
  const reverify = c.verdict==='verified'
    ? `Re-derived from the antecedents below${via} — <b>verified</b>.`
    : (c.verdict==='refuted'
        ? 'The cited antecedents contradicted this value — <b style="color:var(--refuted)">refuted</b>, excluded from the manuscript.'
        : 'No checkable antecedent — <b style="color:var(--unverifiable)">unverifiable</b>, excluded from the manuscript.');
  document.getElementById('detail').innerHTML =
    `<span class="badge ${c.verdict}">${c.verdict}</span>`
    + (c.kind==='quality_caveat' ? '<span class="kind caveat" style="margin-left:8px">quality caveat</span>' : '')
    + `<h2>${esc(c.statement)}</h2>`
    + `<div class="kv"><span class="k">value</span><span class="v">${esc(c.value)}</span></div>`
    + `<div class="reverify">${reverify}</div>`
    + `<p class="prov-h">Antecedents (${c.antecedents.length})</p>${ants}`;
}
select(0);
</script>'''

out = W / "dag_artifact.html"
out.write_text(HTML.replace("__DATA__", DATA))
print(f"wrote {out} ({len(HTML)} bytes template, {stats})")
