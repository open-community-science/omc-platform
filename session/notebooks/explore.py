import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="OMC Data Explorer")


@app.cell
def _():
    import marimo as mo
    import plotly.graph_objects as go
    import pandas as pd
    import numpy as np
    import os
    import json
    import gzip
    from pathlib import Path

    DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
    VIZ_DATA_DIR = Path("/app/viz/data")

    def load_viz(name):
        gz = VIZ_DATA_DIR / f"{name}.json.gz"
        plain = VIZ_DATA_DIR / f"{name}.json"
        if gz.exists():
            with gzip.open(gz, "rt") as f:
                return json.load(f)
        if plain.exists():
            with open(plain) as f:
                return json.load(f)
        return None

    def fmt_bp(bp):
        if bp >= 1e9: return f"{bp/1e9:.1f} Gbp"
        if bp >= 1e6: return f"{bp/1e6:.1f} Mbp"
        if bp >= 1e3: return f"{bp/1e3:.1f} Kbp"
        return f"{bp} bp"

    def load_tsv(path, sep="\t"):
        fp = DATA_DIR / path
        return pd.read_csv(fp, sep=sep) if fp.exists() else None

    return fmt_bp, go, load_viz, mo, np, pd


@app.cell
def _(fmt_bp, go, load_viz, mo, np):
    overview = load_viz("overview")
    cl = load_viz("contig_lengths")

    if not overview:
        overview_tab = mo.md("**No overview data available.** Pipeline may not have completed.")
    else:
        _parts = []
        _parts.append(mo.md(f"""## Assembly Overview

    | Metric | Value |
    |--------|-------|
    | Assembly size | {fmt_bp(overview['assembly_size'])} |
    | N50 | {fmt_bp(overview['n50'])} |
    | Contigs | {overview['n_contigs']:,} |
    | MAGs | {overview['n_mags']} (HQ: {overview['hq']}, MQ: {overview['mq']}, LQ: {overview['lq']}) |
    | Viral contigs | {overview['n_virus']:,} |
    | Plasmid contigs | {overview['n_plasmid']:,} |
    """))

        if cl:
            edges = cl["bin_edges"]
            counts = cl["counts"]
            mids = [(edges[i] + edges[i+1]) / 2 for i in range(len(counts))]

            fig_hist = go.Figure()
            fig_hist.add_trace(go.Bar(
                x=[np.log10(m) for m in mids], y=counts,
                marker_color="#10b981",
                hovertext=[f"{int(m):,} bp: {c}" for m, c in zip(mids, counts)],
                hoverinfo="text",
            ))
            n50 = overview.get("n50", cl.get("n50", 0))
            if n50:
                fig_hist.add_vline(x=np.log10(n50), line_dash="dash", line_color="#f59e0b",
                                   annotation_text=f"N50: {n50:,} bp")
            fig_hist.update_layout(title="Contig Length Distribution",
                                   xaxis_title="Contig length (log10 bp)", yaxis_title="Count",
                                   template="plotly_dark")
            ticks = [100, 1000, 10000, 100000, 1000000]
            fig_hist.update_xaxes(tickvals=[np.log10(t) for t in ticks],
                                  ticktext=["100", "1K", "10K", "100K", "1M"])
            _parts.append(mo.ui.plotly(fig_hist))

            if "scatter_log_length" in cl:
                fig_sc = go.Figure(go.Scattergl(
                    x=cl["scatter_log_length"], y=cl["scatter_log_depth"], mode="markers",
                    marker=dict(size=3, color=cl.get("scatter_gc", [50]*len(cl["scatter_log_length"])),
                                colorscale="Viridis", cmin=20, cmax=80,
                                colorbar=dict(title="GC%")),
                ))
                fig_sc.update_layout(title="Length vs Coverage", xaxis_title="Length (log10 bp)",
                                     yaxis_title="Coverage (log10 x)", template="plotly_dark")
                _parts.append(mo.ui.plotly(fig_sc))

        overview_tab = mo.vstack(_parts)
    return (overview_tab,)


@app.cell
def _(b, fmt_bp, go, load_viz, mo, pd):
    data_q = load_viz("checkm2_all")
    if not data_q:
        quality_tab = mo.md("**No bin quality data.** No MAGs were recovered.")
    else:
        binner_colors = {
            "dastool": "#ef4444", "semibin": "#3b82f6", "metabat": "#10b981",
            "maxbin": "#f59e0b", "lorbin": "#8b5cf6", "comebin": "#ec4899",
            "vamb": "#06b6d4", "vamb_tax": "#84cc16", "binette": "#f97316", "magscot": "#6366f1",
        }
        _fig = go.Figure()
        binners = {}
        for _b in data_q:
            binners.setdefault(_b.get("binner", "unknown"), []).append(b)
        for binner, bins in binners.items():
            _fig.add_trace(go.Scatter(
                x=[_b["contamination"] for _b in bins], y=[_b["completeness"] for _b in bins],
                mode="markers", name=binner,
                marker=dict(size=[max(4, (_b.get("genome_size",1e6)/1e6)**0.5*3) for _b in bins],
                            color=binner_colors.get(binner, "#94a3b8")),
                text=[_b["name"] for _b in bins],
                hovertemplate="%{text}<br>Comp: %{y:.1f}%<br>Cont: %{x:.1f}%<extra>" + binner + "</extra>",
            ))
        _fig.add_shape(type="line", x0=0, x1=5, y0=90, y1=90, line=dict(dash="dash", color="#10b981"))
        _fig.add_shape(type="line", x0=5, x1=5, y0=90, y1=100, line=dict(dash="dash", color="#10b981"))
        _fig.add_shape(type="line", x0=0, x1=10, y0=50, y1=50, line=dict(dash="dash", color="#f59e0b"))
        _fig.add_shape(type="line", x0=10, x1=10, y0=50, y1=100, line=dict(dash="dash", color="#f59e0b"))
        _fig.update_layout(title="Bin Quality: Completeness vs Contamination",
                          xaxis_title="Contamination (%)", yaxis_title="Completeness (%)",
                          template="plotly_dark", yaxis_range=[0, 105])

        df = pd.DataFrame([{
            "Name": _b["name"], "Binner": _b.get("binner",""),
            "Comp%": round(_b["completeness"],1), "Cont%": round(_b["contamination"],1),
            "Size": fmt_bp(_b.get("genome_size",0)), "Contigs": _b.get("n_contigs",0),
        } for _b in data_q]).sort_values("Comp%", ascending=False)

        quality_tab = mo.vstack([mo.ui.plotly(_fig), mo.md("### Bin Quality Table"), mo.ui.table(df)])
    return (quality_tab,)


@app.cell
def _(go, load_viz, mo):
    data_t = load_viz("taxonomy_sunburst")
    if not data_t or not data_t.get("sunbursts"):
        taxonomy_tab = mo.md("**No taxonomy data available.**")
    else:
        source = list(data_t["sunbursts"].keys())[0]
        tree = data_t["sunbursts"][source]

        ids, labels, parents, values = [], [], [], []
        def _walk(node, parent_id=""):
            node_id = f"{parent_id}/{node['name']}" if parent_id else node["name"]
            ids.append(node_id); labels.append(node["name"])
            parents.append(parent_id); values.append(node.get("value", 0))
            for child in node.get("children", []):
                _walk(child, node_id)
        _walk(tree)

        _fig = go.Figure(go.Sunburst(
            ids=ids, labels=labels, parents=parents, values=values,
            branchvalues="total", maxdepth=4,
            hovertemplate="<b>%{label}</b><br>%{value:,} bp<br>%{percentParent:.1%} of parent<extra></extra>",
        ))
        _fig.update_layout(title=f"Community Composition ({source})", template="plotly_dark", height=600)
        taxonomy_tab = mo.ui.plotly(_fig)
    return (taxonomy_tab,)


@app.cell
def _(fig2, go, load_viz, mo):
    data_m = load_viz("mge_summary")
    if not data_m:
        mge_tab = mo.md("**No MGE data available.**")
    else:
        _parts = []
        _parts.append(mo.md(f"""## Mobile Genetic Elements
    | Category | Count |
    |----------|-------|
    | Viral contigs | {data_m.get('n_virus',0):,} |
    | Plasmid contigs | {data_m.get('n_plasmid',0):,} |
    | Defense systems | {data_m.get('n_defense',0):,} |
    | Integron elements | {data_m.get('n_integron_elements',0):,} |
    """))
        defense = data_m.get("defense_types", {})
        if defense:
            sd = sorted(defense.items(), key=lambda x: x[1], reverse=True)
            _fig = go.Figure(go.Bar(x=[d[0] for d in sd], y=[d[1] for d in sd], marker_color="#06b6d4"))
            _fig.update_layout(title="Defense System Types", xaxis_tickangle=-45, template="plotly_dark")
            _parts.append(mo.ui.plotly(_fig))

        viral = data_m.get("viral_taxonomy", {})
        if viral:
            sv = sorted(viral.items(), key=lambda x: x[1], reverse=True)
            _fig2 = go.Figure(go.Pie(labels=[v[0] for v in sv], values=[v[1] for v in sv], hole=0.5))
            _fig2.update_layout(title="Viral Taxonomy (geNomad)", template="plotly_dark")
            _parts.append(mo.ui.plotly(fig2))

        mge_tab = mo.vstack(_parts)
    return (mge_tab,)


@app.cell
def _(b, go, load_viz, mo, np):
    data_cov = load_viz("coverage")
    if not data_cov:
        coverage_tab = mo.md("**No coverage data available.**")
    else:
        bins_c = data_cov["bins"]
        samples = data_cov["sample_names"]
        matrix = data_cov["matrix"]
        log_m = [[np.log10(v + 0.01) for v in row] for row in matrix]

        _fig = go.Figure(go.Heatmap(
            z=log_m, x=samples, y=[b["id"] for _b in bins_c],
            colorscale="Viridis", colorbar=dict(title="log10(depth)"),
            hovertemplate="Bin: %{y}<br>Sample: %{x}<br>Depth: %{customdata:.1f}x<extra></extra>",
            customdata=matrix,
        ))
        _fig.update_layout(title="Coverage Heatmap", template="plotly_dark",
                          height=max(400, len(bins_c) * 15 + 100))
        coverage_tab = mo.ui.plotly(_fig)
    return (coverage_tab,)


@app.cell
def _(go, load_viz, mo):
    data_k = load_viz("kegg_heatmap")
    if not data_k:
        kegg_tab = mo.md("**No KEGG metabolism data available.**")
    else:
        _fig = go.Figure(go.Heatmap(
            z=data_k["matrix"],
            x=[f"{mid}" for mid in data_k["module_ids"]],
            y=data_k["mag_ids"],
            colorscale="Viridis", zmin=0, zmax=1,
            colorbar=dict(title="Completeness"),
            hovertemplate="MAG: %{y}<br>Module: %{x}<br>Completeness: %{z:.0%}<extra></extra>",
        ))
        _fig.update_layout(title="KEGG Module Completeness", template="plotly_dark",
                          height=max(400, len(data_k["mag_ids"]) * 20 + 150),
                          xaxis_tickfont_size=7, xaxis_tickangle=-45)
        kegg_tab = mo.ui.plotly(_fig)
    return (kegg_tab,)


@app.cell
def _(fig2, go, load_viz, mo):
    data_e = load_viz("eukaryotic")
    if not data_e:
        eukaryotic_tab = mo.md("**No eukaryotic detection data available.**")
    else:
        _parts = []
        tiara = data_e.get("tiara_size_weighted", data_e.get("tiara_counts", {}))
        if tiara:
            color_map = {"bacteria": "#06b6d4", "archaea": "#f59e0b", "eukaryota": "#10b981",
                         "organelle": "#8b5cf6", "unknown": "#64748b"}
            _fig = go.Figure(go.Pie(
                labels=list(tiara.keys()), values=list(tiara.values()), hole=0.5,
                marker_colors=[color_map.get(l.lower(), "#94a3b8") for l in tiara.keys()],
            ))
            _fig.update_layout(title="Domain Classification (Tiara)", template="plotly_dark")
            _parts.append(mo.ui.plotly(_fig))

        marferret = data_e.get("marferret_taxonomy", {})
        if marferret:
            sm = sorted(marferret.items(), key=lambda x: x[1], reverse=True)[:15]
            _fig2 = go.Figure(go.Bar(x=[m[0] for m in sm], y=[m[1] for m in sm], marker_color="#8b5cf6"))
            _fig2.update_layout(title="Eukaryotic Phyla (MarFERReT, top 15)", xaxis_tickangle=-45, template="plotly_dark")
            _parts.append(mo.ui.plotly(fig2))

        eukaryotic_tab = mo.vstack(_parts) if _parts else mo.md("*No eukaryotic visualizations.*")
    return (eukaryotic_tab,)


@app.cell
def _(go, load_viz, mo, pd):
    data_b = load_viz("biosynthetic")
    if not data_b or data_b.get("n_regions", 0) == 0:
        biosynthetic_tab = mo.md("**No biosynthetic gene cluster data available.**")
    else:
        _parts = [mo.md(f"## Biosynthetic Gene Clusters\n\n{data_b['n_regions']} BGC regions detected")]
        tc = data_b.get("type_counts", {})
        if tc:
            st = sorted(tc.items(), key=lambda x: x[1], reverse=True)
            _fig = go.Figure(go.Bar(x=[t[0] for t in st], y=[t[1] for t in st], marker_color="#8b5cf6"))
            _fig.update_layout(title="BGC Type Distribution", xaxis_tickangle=-45, template="plotly_dark")
            _parts.append(mo.ui.plotly(_fig))

        regions = data_b.get("regions", [])
        if regions:
            _rows = []
            for r in regions:
                bm = r["known_matches"][0]["description"] if r.get("known_matches") else ""
                sim = f"{r['known_matches'][0]['similarity']*100:.0f}%" if r.get("known_matches") else ""
                _rows.append({"Contig": r["contig"], "Products": ", ".join(r.get("products",[])),
                             "Length": f"{r.get('length',0):,}", "MAG": r.get("mag",""),
                             "Best Match": bm, "Similarity": sim})
            _parts.append(mo.ui.table(pd.DataFrame(_rows)))

        biosynthetic_tab = mo.vstack(_parts)
    return (biosynthetic_tab,)


@app.cell
def _(go, load_viz, mo, palette):
    data_ce = load_viz("contig_explorer")
    if not data_ce or not data_ce.get("contigs"):
        contig_tab = mo.md("**No contig explorer data available.**")
    else:
        contigs = data_ce["contigs"]
        # PCA scatter colored by bin
        categories = {}
        for c in contigs:
            cat = c.get("bin", "") or "unbinned"
            categories.setdefault(cat, []).append(c)
        sorted_cats = sorted(categories.items(), key=lambda x: len(x[1]), reverse=True)
        _palette = ["#ef4444","#3b82f6","#10b981","#f59e0b","#8b5cf6","#ec4899",
                    "#06b6d4","#84cc16","#f97316","#6366f1","#14b8a6","#a855f7",
                    "#e11d48","#0ea5e9","#65a30d","#d946ef","#fb923c","#22d3ee"]

        _fig = go.Figure()
        for i, (cat, cs) in enumerate(sorted_cats[:15]):
            _fig.add_trace(go.Scattergl(
                x=[c.get("pca_x",0) for c in cs], y=[c.get("pca_y",0) for c in cs],
                mode="markers", name=cat[:25],
                marker=dict(size=3, color=_palette[i % len(palette)]),
            ))
        if len(sorted_cats) > 15:
            rest = [c for _,cs in sorted_cats[15:] for c in cs]
            _fig.add_trace(go.Scattergl(
                x=[c.get("pca_x",0) for c in rest], y=[c.get("pca_y",0) for c in rest],
                mode="markers", name="other", marker=dict(size=2, color="#475569"),
            ))
        _fig.update_layout(title=f"Contig Space (PCA, {len(contigs):,} contigs, colored by bin)",
                          xaxis_title="PC1", yaxis_title="PC2", template="plotly_dark", height=600)
        contig_tab = mo.ui.plotly(_fig)
    return (contig_tab,)


@app.cell
def _(b, go, load_viz, mo, palette, pd):
    data_p = load_viz("phylotree")
    if not data_p or not data_p.get("bins"):
        phylo_tab = mo.md("**No GTDB-Tk data available.**")
    else:
        phyla = {}
        _rows = []
        for _b in data_p["bins"]:
            lin = _b.get("lineage", {})
            p = lin.get("phylum", "Unclassified") or "Unclassified"
            phyla[p] = phyla.get(p, 0) + 1
            _rows.append({
                "Bin": _b["name"], "Method": _b.get("method",""),
                "ANI%": f"{b['ani']:.1f}" if _b.get("ani") else "",
                "Domain": lin.get("domain",""), "Phylum": lin.get("phylum",""),
                "Class": lin.get("class",""), "Order": lin.get("order",""),
                "Family": lin.get("family",""), "Genus": lin.get("genus",""),
            })
        sp = sorted(phyla.items(), key=lambda x: x[1], reverse=True)
        _palette = ["#ef4444","#3b82f6","#10b981","#f59e0b","#8b5cf6","#ec4899","#06b6d4","#84cc16"]
        _fig = go.Figure(go.Bar(
            x=[p[0] for p in sp], y=[p[1] for p in sp],
            marker_color=[_palette[i%len(palette)] for i in range(len(sp))],
        ))
        _fig.update_layout(title="Bins by Phylum (GTDB-Tk)", xaxis_tickangle=-45, template="plotly_dark")

        phylo_tab = mo.vstack([mo.ui.plotly(_fig), mo.md("### Classification Table"),
                               mo.ui.table(pd.DataFrame(_rows))])
    return (phylo_tab,)


@app.cell
def _(
    biosynthetic_tab,
    contig_tab,
    coverage_tab,
    eukaryotic_tab,
    kegg_tab,
    mge_tab,
    mo,
    overview_tab,
    phylo_tab,
    quality_tab,
    taxonomy_tab,
):
    mo.ui.tabs({
        "Overview": overview_tab,
        "MAG Quality": quality_tab,
        "Taxonomy": taxonomy_tab,
        "Contigs": contig_tab,
        "Coverage": coverage_tab,
        "KEGG": kegg_tab,
        "MGE": mge_tab,
        "Eukaryotic": eukaryotic_tab,
        "Biosynthetic": biosynthetic_tab,
        "Phylogeny": phylo_tab,
    })
    return


if __name__ == "__main__":
    app.run()
