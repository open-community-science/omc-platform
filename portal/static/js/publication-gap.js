/* Publication-gap widget: aquatic metagenome BioProjects by year, stacked by
   number of associated publications. Data: /static/data/aquatic_publication_gap.json
   (built from the ENA Portal API, Europe PMC and NCBI E-utilities). */
(function () {
  const root = document.getElementById('pubgap');
  if (!root) return;

  const NS = 'http://www.w3.org/2000/svg';
  const W = 900, ML = 62, MR = 22, MT = 42, AXIS_Y = 418;
  const PW = W - ML - MR, PH = AXIS_Y - MT;
  const CATS = [
    { k: '1',  label: '1 paper',  v: '--pg-c1' },
    { k: '2',  label: '2',        v: '--pg-c2' },
    { k: '3+', label: '3+',       v: '--pg-c3' },
    { k: '0',  label: '0 papers', v: '--pg-c0' }
  ];
  const ORDER = ['0', '1', '2', '3+'];

  let ALL = {}, MET = {}, GROUPS = [];
  let grp = 'All aquatic', view = 'nonjgi', metric = 'count';

  const svg   = root.querySelector('#pg-chart');
  const tip   = root.querySelector('#pg-tip');
  const head  = root.querySelector('#pg-headline');
  const box   = root.querySelector('#pg-tablebox');
  const btn   = root.querySelector('#pg-toggle');
  const sel   = root.querySelector('#pg-grp');

  const el = (n, a = {}) => { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); return e; };
  const val = (r, k) => metric === 'count' ? r[k] : r['b' + k];
  const tot = r => metric === 'count' ? r.total : r.btotal;
  const F = v => metric === 'count' ? v.toLocaleString() : (v >= 1 ? v.toFixed(1) : v.toFixed(2)) + ' Tbp';

  function niceMax(v) {
    const steps = metric === 'count'
      ? [10, 25, 50, 100, 250, 500, 1000, 2500, 5000]
      : [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 25, 50, 100, 250];
    for (const s of steps) if (v / s <= 5) return { max: Math.ceil(v / s) * s, step: s };
    const s = steps[steps.length - 1];
    return { max: Math.ceil(v / s) * s, step: s };
  }
  function topRect(x, y, w, h, r) {
    r = Math.min(r, h, w / 2);
    return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
  }
  function headline(key) {
    const m = MET[key]; if (!m) return '';
    if (metric === 'bases')
      return `<b>${m.tbp.toLocaleString()} Tbp</b> of sequence &middot; <b>${m.orph_tbp.toLocaleString()} Tbp never published</b> &middot; only ${m.bpct}% sits in a project with a paper`;
    return `<b>${m.proj.toLocaleString()}</b> projects &middot; <b>${m.pub.toLocaleString()} published (${m.pct}%)</b> &middot; ${(m.proj - m.pub).toLocaleString()} with no paper &middot; <b>${m.orph_tbp.toLocaleString()} Tbp</b> unpublished`;
  }

  function render() {
    const key = grp + '|' + view;
    const DATA = ALL[key] || [];
    svg.textContent = '';
    if (!DATA.length) return;

    const { max: YMAX, step: TICK } = niceMax(Math.max(...DATA.map(tot), 0));
    const band = PW / DATA.length, BARW = Math.min(24, band - 14), off = (band - BARW) / 2;
    const sc = v => AXIS_Y - (v / YMAX) * PH;

    const NT = Math.round(YMAX / TICK);
    for (let i = 0; i <= NT; i++) {
      const v = +(i * TICK).toFixed(6), y = sc(v);
      svg.appendChild(el('line', { x1: ML, x2: W - MR, y1: y, y2: y, stroke: 'var(--pg-grid)', 'stroke-width': 1 }));
      const t = el('text', { x: ML - 10, y: y + 4, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--pg-muted)' });
      t.textContent = metric === 'count' ? v.toLocaleString() : (v === 0 ? '0' : v >= 1 ? v.toFixed(0) : v.toFixed(2));
      svg.appendChild(t);
    }
    svg.appendChild(el('line', { x1: ML, x2: W - MR, y1: AXIS_Y, y2: AXIS_Y, stroke: 'var(--pg-axis)', 'stroke-width': 1 }));
    const yl = el('text', { x: ML - 52, y: 18, 'font-size': 11, fill: 'var(--pg-secondary)', 'font-weight': 600 });
    yl.textContent = metric === 'count' ? 'BioProjects' : 'Sequence (Tbp)';
    svg.appendChild(yl);

    DATA.forEach((row, i) => {
      const x = ML + i * band + off;
      const drawn = CATS.filter(c => val(row, c.k) > 0);
      let cum = 0;
      drawn.forEach((c, j) => {
        const y1 = sc(cum + val(row, c.k)), y0 = sc(cum);
        const isTop = j === drawn.length - 1;
        let h = y0 - y1; if (!isTop) h = Math.max(h - 2, 1);
        const yy = y0 - h;
        const node = isTop
          ? el('path', { d: topRect(x, yy, BARW, h, 4), fill: `var(${c.v})`, class: 'pg-seg' })
          : el('rect', { x, y: yy, width: BARW, height: h, fill: `var(${c.v})`, class: 'pg-seg' });
        node.addEventListener('mousemove', e => showTip(e, row, c.k));
        node.addEventListener('mouseleave', hideTip);
        svg.appendChild(node);
        cum += val(row, c.k);
      });
      const t = el('text', { x: x + BARW / 2, y: AXIS_Y + 17, 'text-anchor': 'middle', 'font-size': 11, fill: 'var(--pg-secondary)' });
      t.textContent = row.year; svg.appendChild(t);
    });

    head.innerHTML = headline(key);
    if (!box.hasAttribute('hidden')) buildTable();
  }

  function showTip(e, row, active) {
    const rows = ORDER.map(k => {
      const c = CATS.find(x => x.k === k);
      return `<tr class="${k === active ? 'on' : ''}"><td><span class="pg-dot" style="background:var(${c.v})"></span>${c.label}</td><td>${F(val(row, k))}</td></tr>`;
    }).join('');
    tip.innerHTML = `<b>${row.year}</b> &middot; ${metric === 'count' ? row.total.toLocaleString() + ' projects' : F(row.btotal)}`
      + `<table>${rows}</table><div class="pg-tip-foot">${metric === 'count' ? row.pct_pub : row.bpct_pub}% published</div>`;
    tip.style.opacity = 1;
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(e.clientX + pad, window.innerWidth - w - 8) + 'px';
    tip.style.top = Math.max(8, Math.min(e.clientY + pad, window.innerHeight - h - 8)) + 'px';
  }
  const hideTip = () => { tip.style.opacity = 0; };

  function buildTable() {
    const DATA = ALL[grp + '|' + view] || [];
    const unit = metric === 'count' ? 'BioProjects' : 'Tbp';
    const head_ = `<tr><th>Year (${unit})</th><th>0 papers</th><th>1</th><th>2</th><th>3+</th><th>Total</th><th>% published</th></tr>`;
    const body = DATA.map(r => `<tr><td>${r.year}</td>` + ORDER.map(k => `<td>${F(val(r, k))}</td>`).join('')
      + `<td>${F(tot(r))}</td><td>${metric === 'count' ? r.pct_pub : r.bpct_pub}%</td></tr>`).join('');
    const T = DATA.reduce((a, r) => { ORDER.forEach(k => a[k] = (a[k] || 0) + val(r, k)); a.total = (a.total || 0) + tot(r); return a; }, {});
    const foot = `<tr class="pg-total"><td>All years</td>` + ORDER.map(k => `<td>${F(T[k])}</td>`).join('')
      + `<td>${F(T.total)}</td><td>${(100 * (T.total - T['0']) / T.total).toFixed(1)}%</td></tr>`;
    box.innerHTML = `<table class="pg-data">${head_}${body}${foot}</table>`;
  }

  root.querySelectorAll('.pg-seg-ctl button').forEach(b => {
    b.addEventListener('click', () => {
      const group = b.parentElement;
      if (b.dataset.view) view = b.dataset.view;
      if (b.dataset.metric) metric = b.dataset.metric;
      group.querySelectorAll('button').forEach(o => o.setAttribute('aria-pressed', String(o === b)));
      render();
    });
  });
  btn.addEventListener('click', () => {
    const open = box.hasAttribute('hidden');
    if (open) { buildTable(); box.removeAttribute('hidden'); } else box.setAttribute('hidden', '');
    btn.textContent = open ? 'Hide data table' : 'Show data table';
    btn.setAttribute('aria-expanded', String(open));
  });

  fetch('/static/data/aquatic_publication_gap.json')
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(b => {
      ALL = b.data; MET = b.meta; GROUPS = b.groups;
      GROUPS.forEach(g => { const o = document.createElement('option'); o.value = g; o.textContent = g; sel.appendChild(o); });
      sel.value = grp;
      sel.addEventListener('change', () => { grp = sel.value; render(); });
      render();
    })
    .catch(() => { root.querySelector('.pg-body').innerHTML =
      '<p class="pg-fallback">Publication-gap figures are temporarily unavailable.</p>'; });
})();
