"""Live view of a hyphal autoresearch run (#58).

Watches a run's log and renders the colony as it grows: each investigation is a tip,
follow-ups branch off the tip that raised them, and claims bud along the way.

    python tests/bench/hyphal_viz.py --log /path/to/run.log
    python tests/bench/hyphal_viz.py --log run.log --port 8765 --open

Then open http://localhost:8765. The page polls while the run is going and keeps
working after it ends — the same log becomes the record of what happened.

Stdlib only, and read-only on the log: a viewer must never be able to disturb the
run it is watching. Parsing happens per request; these logs are kilobytes.
"""
import argparse
import http.server
import json
import os
import re
import socketserver
import time
import webbrowser
from pathlib import Path

# ── log grammar ───────────────────────────────────────────────────────────────
# Emitted by results_explorer._print_progress. Kept as one table so the shape of
# the log and the shape of the parser stay visibly in step.
RE_PHASE = re.compile(r"^=== (.+?) ===")
RE_GERM = re.compile(r"^\s*germinating agenda")
RE_TIP = re.compile(r"^\s*▸\s+(a\d+)(?:\s*⤶\s*(a\d+))?:\s*(.*?)\s*\((\d+) claims in hand\)\s*$")
RE_TIP_DONE = re.compile(r"^\s*✓\s+(a\d+)\s+(\w+)\s*\((\d+) claims\)")
RE_CLAIM = re.compile(r"^\s*\+\s+(k\d+)\s*(.*)$")
RE_ANALYSIS = re.compile(r"^\s*·\s+(\S+)\s*(.*)$")
RE_FOLLOWUP = re.compile(r"^\s*↳\s+(a\d+):\s*(.*)$")
RE_SWEEP = re.compile(r"^\s*sweeping\s+(\d+) claims")
RE_DONE = re.compile(r"^\s*(\d+) tips, (\d+) claims, (\d+) steps")
RE_VERDICT = re.compile(r"^\s*\[(\w+)\s*\]\s+(k\d+)\s+(.*?)\s{2,}(.*)$")
RE_AGENDA = re.compile(r"^\s*(?:•|└─)\s*\[(\w+)\]\s*(a\d+):\s*(.*)$")
RE_MODEL = re.compile(r"^model:\s*(.+?)\s*@")
RE_SUBMISSION = re.compile(r"^submission\s+(\S+):\s*(.*)$")


def parse(text: str) -> dict:
    """Log → colony state.

    Statuses are only as good as the log: while a run is going, a tip that has been
    left behind is reported as `grown` rather than `done`, because until the final
    agenda dump nothing distinguishes a finished tip from an abandoned one. Runs from
    a build that emits `tip_done` say which, live. Guessing would make an interrupted
    investigation look completed, which is the one lie this whole subsystem exists to
    prevent."""
    tips: dict[str, dict] = {}
    claims: list[dict] = []
    events: list[dict] = []
    state = {"phase": "starting", "model": None, "submission": None, "title": None,
             "sweep": None, "totals": None, "order": []}
    current = None

    def tip(tid, **kw):
        t = tips.setdefault(tid, {"id": tid, "parent": None, "question": "",
                                  "status": "pending", "claims": [], "seeded_with": None,
                                  "analyses": 0, "seq": len(tips)})
        t.update({k: v for k, v in kw.items() if v is not None})
        return t

    for line in text.splitlines():
        if m := RE_SUBMISSION.match(line):
            state["submission"], state["title"] = m.group(1), m.group(2)
        elif m := RE_MODEL.match(line):
            state["model"] = m.group(1)
        elif m := RE_PHASE.match(line):
            state["phase"] = m.group(1)
            events.append({"kind": "phase", "text": m.group(1)})
        elif RE_GERM.match(line):
            events.append({"kind": "germinate", "text": "germinating agenda"})
        elif m := RE_TIP.match(line):
            tid, parent, question, seen = m.groups()
            if current and tips.get(current, {}).get("status") == "growing":
                tips[current]["status"] = "grown"
            current = tid
            tip(tid, parent=parent, question=question, status="growing",
                seeded_with=int(seen))
            state["order"].append(tid)
            events.append({"kind": "tip", "id": tid, "text": question})
        elif m := RE_TIP_DONE.match(line):
            tid, status, _n = m.groups()
            tip(tid, status=status)
            if current == tid:
                current = None
            events.append({"kind": "tip_done", "id": tid, "text": status})
        elif m := RE_ANALYSIS.match(line):
            cid, label = m.groups()
            if current:
                tip(current)["analyses"] = tip(current).get("analyses", 0) + 1
            events.append({"kind": "analysis", "id": cid, "text": label})
        elif m := RE_CLAIM.match(line):
            kid, statement = m.groups()
            claims.append({"id": kid, "tip": current, "statement": statement,
                           "verdict": None})
            if current:
                tip(current)["claims"].append(kid)
            events.append({"kind": "claim", "id": kid, "text": statement})
        elif m := RE_FOLLOWUP.match(line):
            tid, question = m.groups()
            tip(tid, parent=current, question=question, status="pending")
            events.append({"kind": "followup", "id": tid, "text": question})
        elif m := RE_SWEEP.match(line):
            if current and tips.get(current, {}).get("status") == "growing":
                tips[current]["status"] = "grown"
            current = None
            state["sweep"] = int(m.group(1))
            events.append({"kind": "sweep", "text": f"sweeping {m.group(1)} claims"})
        elif m := RE_DONE.match(line):
            state["totals"] = {"tips": int(m.group(1)), "claims": int(m.group(2)),
                               "steps": int(m.group(3))}
        elif m := RE_AGENDA.match(line):
            # The end-of-run dump: the first place done and interrupted are told apart.
            status, tid, question = m.groups()
            tip(tid, status=status, question=question)
        elif m := RE_VERDICT.match(line):
            verdict, kid = m.group(1), m.group(2)
            for c in claims:
                if c["id"] == kid:
                    c["verdict"] = verdict

    by_id = {c["id"]: c for c in claims}
    for t in tips.values():
        t["claim_detail"] = [by_id[k] for k in t["claims"] if k in by_id]
    return {**state, "tips": list(tips.values()), "claims": claims,
            "events": events[-60:], "active": current}


def enrich(st: dict, ledger: dict) -> dict:
    """Overlay the finished run's ledger onto what the log could show.

    The log is a progress feed, not a record — it carries whatever the printer chose
    to print. The ledger is the record. Once a run has written one, prefer it: full
    claim statements, full questions, real per-item statuses, and verdicts. Anything
    the ledger does not mention is left exactly as the log had it, so this can only
    add detail, never quietly drop a tip the ledger never knew about."""
    claims = {c["id"]: c for c in ledger.get("claims", []) if c.get("id")}
    agenda = {a["id"]: a for a in ledger.get("agenda", []) if a.get("id")}
    # Claims recorded during a phase the printer said nothing about are still claims.
    seen = {c["id"] for c in st["claims"]}
    for kid, lc in claims.items():
        if kid not in seen:
            st["claims"].append({"id": kid, "tip": lc.get("investigation"),
                                 "statement": lc.get("statement", ""), "verdict": None})
    by_tip: dict[str, list] = {}
    for c in st["claims"]:
        if lc := claims.get(c["id"]):
            c["statement"] = lc.get("statement") or c["statement"]
            c["verdict"] = lc.get("verdict") or c["verdict"]
            c["value"] = lc.get("value")
            c["tip"] = lc.get("investigation") or c["tip"]
        by_tip.setdefault(c["tip"], []).append(c)
    for t in st["tips"]:
        if la := agenda.get(t["id"]):
            t["question"] = la.get("question") or t["question"]
            t["status"] = la.get("status") or t["status"]
            t["parent"] = la.get("parent", t["parent"])
        t["claim_detail"] = by_tip.get(t["id"], t.get("claim_detail", []))
        t["claims"] = [c["id"] for c in t["claim_detail"]]
    # Investigations the log never saw start (never grown) still belong on the colony.
    known = {t["id"] for t in st["tips"]}
    for aid, la in agenda.items():
        if aid not in known:
            detail = by_tip.get(aid, [])
            st["tips"].append({"id": aid, "parent": la.get("parent"),
                               "question": la.get("question", ""),
                               "status": la.get("status", "pending"),
                               "claims": [c["id"] for c in detail],
                               "claim_detail": detail, "seeded_with": None, "analyses": 0,
                               "seq": len(st["tips"])})
    st["assumptions"] = ledger.get("assumptions", [])
    st["run"] = ledger.get("run", {})
    st["from_ledger"] = True
    return st


def read_state(log: Path, pid: int | None = None, ledger: Path | None = None) -> dict:
    try:
        text = log.read_text(errors="replace")
        mtime = os.path.getmtime(log)
    except OSError as e:
        return {"error": str(e), "tips": [], "claims": [], "events": [],
                "phase": "no log", "order": []}
    st = parse(text)
    st["log"] = str(log)
    st["idle_s"] = round(time.time() - mtime)
    st["bytes"] = len(text)
    # A quiet log is normal — a tip can spend many minutes inside run_analysis. Only
    # the process itself can say whether quiet means working or gone.
    st["alive"] = _alive(pid) if pid else None
    st["from_ledger"] = False
    if src := _record_path(ledger):
        try:
            st = enrich(st, json.loads(src.read_text()))
        except (OSError, ValueError):
            pass          # not written yet, or mid-rename — the log still stands
    return st


def _record_path(ledger: Path | None) -> Path | None:
    """The run's own record, if it has written one.

    Given a directory, prefer the finished ``claims_ledger.json`` and fall back to the
    live ``run_state.json`` a run publishes as it goes — so one invocation covers a
    run in progress and the same run tomorrow."""
    if ledger is None:
        return None
    if ledger.is_dir():
        for name in ("claims_ledger.json", "run_state.json"):
            if (p := ledger / name).exists():
                return p
        return None
    return ledger if ledger.exists() else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>hyphal colony — autoresearch</title>
<style>
  :root {
    --bg:#0c0f0d; --panel:#141a17; --line:#22302a; --ink:#d8e6de; --dim:#7e948a;
    --grow:#6ee7a8; --done:#4a9d78; --pend:#3a4d45; --intr:#d99b52; --claim:#a8e6cf;
    --refute:#e06c75;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f6f8f6; --panel:#fff; --line:#dbe5df; --ink:#17251f; --dim:#5d726a;
            --grow:#0f9d58; --done:#2f7d5c; --pend:#c2d2c9; --intr:#b26a12;
            --claim:#2f7d5c; --refute:#c0392b; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:13px/1.5 ui-monospace,
         SFMono-Regular,Menlo,Consolas,monospace; height:100vh; overflow:hidden; }
  header { display:flex; gap:18px; align-items:baseline; flex-wrap:wrap;
           padding:10px 16px; border-bottom:1px solid var(--line); }
  h1 { font-size:14px; margin:0; font-weight:600; letter-spacing:.02em; }
  .meta { color:var(--dim); font-size:12px; }
  .stat b { color:var(--grow); font-weight:600; }
  /* The side pane holds prose, not labels — let it take a real share of a wide
     screen instead of a fixed 340px column. */
  main { display:grid; grid-template-columns:1fr minmax(340px, 30vw);
         height:calc(100vh - 45px); }
  @media (max-width:900px){ main{grid-template-columns:1fr; grid-template-rows:1fr auto;} }
  #stage { position:relative; overflow:hidden; }
  aside { border-left:1px solid var(--line); background:var(--panel); overflow-y:auto;
          padding:14px; }
  aside h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
             color:var(--dim); margin:0 0 8px; font-weight:600; }
  .q { margin:0 0 12px; line-height:1.6; }
  .claim { border-left:2px solid var(--claim); padding:2px 0 2px 8px; margin:6px 0;
           color:var(--ink); }
  .claim .cid { color:var(--dim); }
  .claim.refuted, .claim.overturned { border-color:var(--refute); }
  /* Wrap, never clip. These lines carry whole agenda questions — several hundred
     characters of the actual content — and an ellipsis at the pane edge hides the
     part worth reading. Hanging indent keeps the icon column legible. */
  .ev { color:var(--dim); padding:2px 0 2px 15px; text-indent:-15px;
        overflow-wrap:anywhere; }
  .ev b { color:var(--ink); font-weight:500; }
  .legend { display:flex; gap:14px; flex-wrap:wrap; padding:8px 16px; font-size:11px;
            color:var(--dim); border-top:1px solid var(--line); }
  .swatch { display:inline-block; width:9px; height:9px; border-radius:50%;
            margin-right:5px; vertical-align:-1px; }
  svg { width:100%; height:100%; display:block; cursor:grab; }
  svg.drag { cursor:grabbing; }
  .edge { fill:none; stroke:var(--pend); stroke-width:1.6; opacity:.75; }
  .edge.lineage { stroke:var(--grow); stroke-width:2.4; opacity:1; }
  .node circle.body { transition:r .4s ease, fill .4s ease, opacity .4s ease; }
  .node text { font-size:10px; fill:var(--dim); pointer-events:none;
               transition:fill .3s ease; }
  .node.sel text, .node:hover text { fill:var(--ink); }
  .node { cursor:pointer; }
  .bud { transition:opacity .5s ease; }
  .pulse { animation:pulse 1.9s ease-in-out infinite; transform-origin:center; }
  @keyframes pulse { 0%,100%{opacity:.25; r:26} 50%{opacity:0; r:40} }
  button { background:none; border:1px solid var(--line); color:var(--dim);
           font:inherit; font-size:11px; padding:3px 9px; border-radius:3px;
           cursor:pointer; }
  button:hover { color:var(--ink); border-color:var(--dim); }
  .empty { color:var(--dim); }
  .dead { color:var(--intr); }
  .quiet { color:var(--dim); }
</style>
<header>
  <h1>hyphal colony</h1>
  <span class="meta" id="sub"></span>
  <span class="stat" id="stats"></span>
  <span class="meta" id="idle"></span>
  <button id="fit">fit</button>
</header>
<main>
  <div id="stage"><svg id="svg"></svg></div>
  <aside>
    <h2 id="dtitle">colony</h2>
    <div id="detail"></div>
    <h2 style="margin-top:18px">activity</h2>
    <div id="events"></div>
  </aside>
</main>
<div class="legend">
  <span><i class="swatch" style="background:var(--grow)"></i>growing</span>
  <span><i class="swatch" style="background:var(--done)"></i>done</span>
  <span><i class="swatch" style="background:var(--intr)"></i>interrupted</span>
  <span><i class="swatch" style="background:var(--pend)"></i>pending</span>
  <span><i class="swatch" style="background:var(--claim)"></i>claim</span>
  <span id="note">while a run is going, a tip left behind reads as <b>grown</b> — done vs interrupted is only known at the end</span>
</div>
<script>
const SVG = document.getElementById('svg');
const NS = 'http://www.w3.org/2000/svg';
let sel = null, view = {x:0, y:0, k:1}, userMoved = false, last = null, lastSig = null;

const el = (n, a={}) => { const e = document.createElementNS(NS, n);
  for (const [k,v] of Object.entries(a)) e.setAttribute(k, v); return e; };
const esc = s => (s??'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

/* Radial layout: a virtual spore at the centre, generations as rings, each subtree
   given an angular slice proportional to how much of the colony it accounts for. */
function layout(tips) {
  const byId = Object.fromEntries(tips.map(t => [t.id, t]));
  /* Children and subtree sizes are indexed once. Filtering the whole array per node
     and recomputing sizes on every recursion is fine at ten tips and silly at forty. */
  const kidsOf = new Map(tips.map(t => [t.id, []]));
  const roots = [];
  for (const t of tips) {
    const k = kidsOf.get(t.parent);
    (t.parent && k ? k : roots).push(t);
  }
  const kids = id => kidsOf.get(id) || [];
  const sizes = new Map();
  const size = t => {
    if (sizes.has(t.id)) return sizes.get(t.id);
    sizes.set(t.id, 1);                       // guards a malformed parent cycle
    const n = 1 + kids(t.id).reduce((a, k) => a + size(k), 0);
    sizes.set(t.id, n);
    return n;
  };
  const pos = {};
  const place = (t, a0, a1, depth) => {
    const a = (a0 + a1) / 2, r = depth === 0 ? 0 : 96 + (depth - 1) * 118;
    pos[t.id] = {x: Math.cos(a) * r, y: Math.sin(a) * r, a, r, depth};
    const ks = kids(t.id); if (!ks.length) return;
    const total = ks.reduce((n, k) => n + size(k), 0);
    /* children fan out around the parent's own bearing, never a full circle */
    const span = Math.min(a1 - a0, Math.PI * 0.9), start = a - span / 2;
    let acc = 0;
    for (const k of ks) {
      const w = size(k) / total * span;
      place(k, start + acc, start + acc + w, depth + 1);
      acc += w;
    }
  };
  const total = roots.reduce((n, t) => n + size(t), 0) || 1;
  let acc = 0;
  for (const t of roots) {
    const w = size(t) / total * Math.PI * 2;
    place(t, acc - Math.PI / 2, acc + w - Math.PI / 2, 1);
    acc += w;
  }
  return pos;
}

const COLOR = {growing:'var(--grow)', grown:'var(--done)', done:'var(--done)',
               interrupted:'var(--intr)', pending:'var(--pend)', in_progress:'var(--grow)'};

function render(st) {
  const tips = st.tips || [];
  SVG.textContent = '';
  const root = el('g', {id:'cam'});
  SVG.appendChild(root);
  if (!tips.length) {
    const t = el('text', {x:0, y:0, 'text-anchor':'middle', fill:'var(--dim)'});
    t.textContent = st.phase === 'no log' ? 'no log yet' : 'germinating…';
    root.appendChild(t);
    apply(); return;
  }
  const pos = layout(tips);
  const byId = Object.fromEntries(tips.map(t => [t.id, t]));
  const lineage = id => { const out = new Set(); let c = byId[id];
    while (c) { out.add(c.id); c = byId[c.parent]; } return out; };
  const hot = sel ? lineage(sel) : new Set();

  const edges = el('g'); root.appendChild(edges);
  for (const t of tips) {
    const p = pos[t.id], q = t.parent && pos[t.parent];
    const from = q || {x:0, y:0};
    /* bow each hypha off the straight line so branches read as growth, not wiring */
    const mx = (from.x + p.x) / 2, my = (from.y + p.y) / 2;
    const nx = -(p.y - from.y) * 0.16, ny = (p.x - from.x) * 0.16;
    const on = hot.has(t.id) && (!t.parent || hot.has(t.parent));
    edges.appendChild(el('path', {class:'edge' + (on ? ' lineage' : ''),
      d:`M${from.x},${from.y} Q${mx+nx},${my+ny} ${p.x},${p.y}`}));
  }
  root.appendChild(el('circle', {cx:0, cy:0, r:5, fill:'var(--dim)'}));

  for (const t of tips) {
    const p = pos[t.id], n = (t.claims || []).length;
    const g = el('g', {class:'node' + (sel === t.id ? ' sel' : ''),
                       transform:`translate(${p.x},${p.y})`});
    g.addEventListener('click', e => { e.stopPropagation(); sel = t.id; paint(); });
    if (t.status === 'growing' || t.status === 'in_progress')
      g.appendChild(el('circle', {class:'pulse', r:26, fill:'var(--grow)', opacity:.25}));
    /* claims bud around the tip that recorded them */
    (t.claim_detail || []).forEach((c, i) => {
      const a = (i / Math.max(n, 1)) * Math.PI * 2 - Math.PI / 2, r = 21;
      g.appendChild(el('circle', {class:'bud', cx:Math.cos(a)*r, cy:Math.sin(a)*r, r:3.4,
        fill:['refuted','overturned'].includes(c.verdict) ? 'var(--refute)' : 'var(--claim)'}));
    });
    g.appendChild(el('circle', {class:'body', r:9 + Math.min(n, 8) * 0.9,
      fill:COLOR[t.status] || 'var(--pend)',
      stroke:sel === t.id ? 'var(--ink)' : 'none', 'stroke-width':1.5}));
    const label = el('text', {y:-26, 'text-anchor':'middle'});
    label.textContent = t.id + (n ? ` · ${n}` : '');
    g.appendChild(label);
    root.appendChild(g);
  }
  if (!userMoved) fit(pos);
  apply();
}

function fit(pos) {
  const xs = Object.values(pos).map(p => p.x), ys = Object.values(pos).map(p => p.y);
  const pad = 70;
  const x0 = Math.min(0, ...xs) - pad, x1 = Math.max(0, ...xs) + pad;
  const y0 = Math.min(0, ...ys) - pad, y1 = Math.max(0, ...ys) + pad;
  const r = SVG.getBoundingClientRect();
  view.k = Math.min(r.width / (x1 - x0), r.height / (y1 - y0), 1.6);
  view.x = r.width / 2 - ((x0 + x1) / 2) * view.k;
  view.y = r.height / 2 - ((y0 + y1) / 2) * view.k;
}
const apply = () => { const c = document.getElementById('cam');
  if (c) c.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.k})`); };

/* pan + zoom */
let drag = null;
SVG.addEventListener('mousedown', e => { drag = {x:e.clientX - view.x, y:e.clientY - view.y};
  SVG.classList.add('drag'); });
addEventListener('mouseup', () => { drag = null; SVG.classList.remove('drag'); });
addEventListener('mousemove', e => { if (!drag) return; userMoved = true;
  view.x = e.clientX - drag.x; view.y = e.clientY - drag.y; apply(); });
SVG.addEventListener('wheel', e => { e.preventDefault(); userMoved = true;
  const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
  const r = SVG.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  view.x = mx - (mx - view.x) * f; view.y = my - (my - view.y) * f; view.k *= f; apply();
}, {passive:false});
SVG.addEventListener('click', () => { sel = null; paint(); });
document.getElementById('fit').onclick = () => { userMoved = false; paint(); };

/* Selection is a local interaction; it must never wait on the poll. Everything that
   changes `sel` repaints immediately, and the poll only repaints when the RUN moved. */
function paint() { if (!last) return; render(last); panel(last); }

function panel(st) {
  const d = document.getElementById('detail'), h = document.getElementById('dtitle');
  const t = (st.tips || []).find(x => x.id === sel);
  if (!t) {
    h.textContent = 'colony';
    const done = (st.tips||[]).filter(x => x.status === 'done').length;
    d.innerHTML = `<p class="q">${esc(st.title || '')}</p>`
      + `<div class="ev">model <b>${esc(st.model || '?')}</b></div>`
      + `<div class="ev">phase <b>${esc(st.phase || '')}</b></div>`
      + (done ? `<div class="ev"><b>${done}</b> investigations done</div>` : '')
      + `<div class="ev" style="margin-top:8px">click a tip to see its question and claims</div>`;
    return;
  }
  h.textContent = t.id + (t.parent ? ` · branched from ${t.parent}` : '');
  const cl = (t.claim_detail || []).map(c =>
    `<div class="claim ${c.verdict||''}"><span class="cid">${c.id}${
      c.verdict ? ' · ' + c.verdict : ''}</span><br>${esc(c.statement)}</div>`).join('')
    || '<div class="empty">no claims recorded</div>';
  d.innerHTML = `<p class="q">${esc(t.question)}</p>`
    + `<div class="ev">status <b>${esc(t.status)}</b></div>`
    + (t.seeded_with != null
        ? `<div class="ev">seeded with <b>${t.seeded_with}</b> claims in hand</div>` : '')
    + `<h2 style="margin-top:14px">claims</h2>${cl}`;
}

const ICON = {tip:'▸', claim:'+', followup:'↳', phase:'—', sweep:'∴',
              germinate:'∘', tip_done:'✓', analysis:'·'};
function ticker(st) {
  /* The full text goes in the DOM and the CSS ellipsis handles the width, so nothing
     is lost — hover (or widen the pane) to read a line in full. */
  document.getElementById('events').innerHTML =
    (st.events || []).slice().reverse().map(e =>
      `<div class="ev" title="${esc(e.text || '')}">${ICON[e.kind] || '·'} ${
        e.id ? `<b>${e.id}</b> ` : ''}${esc(e.text || '')}</div>`).join('')
    || '<div class="empty">…</div>';
}

async function poll() {
  try {
    const st = await (await fetch('state.json')).json();
    last = st;
    document.getElementById('sub').textContent = st.submission || '';
    const nt = (st.tips || []).length, nc = (st.claims || []).length;
    const growing = (st.tips || []).filter(t => t.status === 'growing').length;
    document.getElementById('stats').innerHTML =
      `<b>${nt}</b> tips · <b>${nc}</b> claims${growing ? ` · <b>${growing}</b> growing` : ''}`;
    /* A quiet log is normal — a tip inside run_analysis prints nothing for minutes.
       Only the pid says whether quiet means working or gone, so never dress one up
       as the other. */
    const idle = st.idle_s ?? 0;
    const ago = idle < 90 ? `${idle}s` : `${Math.floor(idle / 60)}m`;
    const live = st.alive === false ? 'run ended' : st.alive ? 'running' : '';
    document.getElementById('idle').innerHTML =
      `<span class="${st.alive === false ? 'dead' : ''}">${live}</span>`
      + `<span class="quiet"> · last line ${ago} ago</span>`;
    /* Only rebuild when the RUN changed. Repainting every 3s regardless threw away
       whatever you were reading — text selection, hover, scroll position in the
       ticker — for a poll that usually returns the same colony. */
    const sig = JSON.stringify([st.phase, st.alive, st.from_ledger, st.tips,
                                st.claims, st.events]);
    if (sig !== lastSig) { lastSig = sig; render(st); panel(st); ticker(st); }
  } catch (e) { document.getElementById('idle').textContent = 'viewer disconnected'; }
}
poll(); setInterval(poll, 3000);
addEventListener('resize', () => { if (!userMoved) paint(); });
</script>
"""


def serve(log: Path, port: int, open_browser: bool, pid: int | None = None,
          ledger: Path | None = None) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/state.json"):
                body = json.dumps(read_state(log, pid, ledger), default=str).encode()
                ctype = "application/json"
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass          # a poll every 3s would bury the console it shares

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        url = f"http://localhost:{port}"
        print(f"hyphal viz on {url}  (watching {log})", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", required=True, type=Path, help="run log to watch")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="open a browser")
    ap.add_argument("--pid", type=int, help="run's pid, so a quiet log can be told "
                                            "from a dead one")
    ap.add_argument("--ledger", type=Path, help="the run's output dir (or a ledger "
                                                "file). Its own record replaces what "
                                                "the log could only summarise — live "
                                                "from run_state.json, final from "
                                                "claims_ledger.json")
    ap.add_argument("--dump", action="store_true", help="print parsed state and exit")
    a = ap.parse_args()
    if a.dump:
        print(json.dumps(read_state(a.log, a.pid, a.ledger), indent=2, default=str))
        return
    serve(a.log, a.port, a.open, a.pid, a.ledger)


if __name__ == "__main__":
    main()
