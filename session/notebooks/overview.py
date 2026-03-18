import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="Overview")


@app.cell
def _():
    import marimo as mo
    import plotly.graph_objects as go
    import numpy as np
    from data_utils import load_viz_json, fmt_bp
    return go, mo, np, load_viz_json, fmt_bp


@app.cell
def _(mo, load_viz_json, fmt_bp):
    overview = load_viz_json("overview")
    if not overview:
        mo.stop(True, mo.md("**No overview data available.** Pipeline may not have completed."))

    mo.md(f"""# Assembly Overview

| Metric | Value |
|--------|-------|
| Assembly size | {fmt_bp(overview['assembly_size'])} |
| N50 | {fmt_bp(overview['n50'])} |
| Contigs | {overview['n_contigs']:,} |
| MAGs | {overview['n_mags']} (HQ: {overview['hq']}, MQ: {overview['mq']}, LQ: {overview['lq']}) |
| Viral contigs | {overview['n_virus']:,} |
| Plasmid contigs | {overview['n_plasmid']:,} |
""")
    return (overview,)


@app.cell
def _(mo, go, np, load_viz_json, overview):
    cl = load_viz_json("contig_lengths")
    if not cl:
        mo.stop(True, mo.md("*No contig length data.*"))

    edges = cl["bin_edges"]
    counts = cl["counts"]
    # Use midpoints for bar chart x-axis
    mids = [(edges[i] + edges[i+1]) / 2 for i in range(len(counts))]

    fig_hist = go.Figure()
    fig_hist.add_trace(go.Bar(
        x=[np.log10(m) for m in mids],
        y=counts,
        marker_color="#10b981",
        hovertext=[f"{int(m):,} bp: {c}" for m, c in zip(mids, counts)],
        hoverinfo="text",
    ))
    # N50 line
    n50 = overview.get("n50", cl.get("n50", 0))
    if n50:
        fig_hist.add_vline(x=np.log10(n50), line_dash="dash", line_color="#f59e0b",
                           annotation_text=f"N50: {n50:,} bp")
    fig_hist.update_layout(
        title="Contig Length Distribution",
        xaxis_title="Contig length (log10 bp)",
        yaxis_title="Count",
        template="plotly_dark",
    )
    # Custom tick labels
    ticks = [100, 1000, 10000, 100000, 1000000]
    fig_hist.update_xaxes(tickvals=[np.log10(t) for t in ticks],
                          ticktext=["100", "1K", "10K", "100K", "1M"])
    mo.ui.plotly(fig_hist)
    return (cl,)


@app.cell
def _(mo, go, np, cl):
    if not cl or "scatter_log_length" not in cl:
        mo.stop(True, mo.md("*No length vs coverage scatter data.*"))

    fig_scatter = go.Figure()
    fig_scatter.add_trace(go.Scattergl(
        x=cl["scatter_log_length"],
        y=cl["scatter_log_depth"],
        mode="markers",
        marker=dict(
            size=3,
            color=cl.get("scatter_gc", [50] * len(cl["scatter_log_length"])),
            colorscale="Viridis",
            cmin=20, cmax=80,
            colorbar=dict(title="GC%"),
        ),
        hovertemplate="Length: %{customdata[0]}<br>Depth: %{customdata[1]:.1f}x<br>GC: %{customdata[2]:.1f}%",
        customdata=list(zip(
            [f"{10**v:.0f}" for v in cl["scatter_log_length"]],
            [10**v for v in cl["scatter_log_depth"]],
            cl.get("scatter_gc", [0] * len(cl["scatter_log_length"])),
        )),
    ))
    fig_scatter.update_layout(
        title="Contig Length vs Coverage",
        xaxis_title="Length (log10 bp)",
        yaxis_title="Coverage (log10 x)",
        template="plotly_dark",
    )
    ticks_l = [1000, 10000, 100000, 1000000]
    fig_scatter.update_xaxes(tickvals=[np.log10(t) for t in ticks_l],
                             ticktext=["1K", "10K", "100K", "1M"])
    ticks_d = [0.1, 1, 10, 100, 1000]
    fig_scatter.update_yaxes(tickvals=[np.log10(t) for t in ticks_d],
                             ticktext=["0.1x", "1x", "10x", "100x", "1000x"])
    mo.ui.plotly(fig_scatter)
    return


@app.cell
def _(mo, go, cl):
    if not cl or "cov_log_edges" not in cl:
        mo.stop(True, mo.md("*No coverage distribution data.*"))

    edges_c = cl["cov_log_edges"]
    counts_c = cl["cov_counts"]
    mids_c = [(edges_c[i] + edges_c[i+1]) / 2 for i in range(len(counts_c))]

    fig_cov = go.Figure()
    fig_cov.add_trace(go.Bar(
        x=mids_c,
        y=counts_c,
        marker_color="#06b6d4",
        hovertext=[f"{10**m:.1f}x: {c}" for m, c in zip(mids_c, counts_c)],
        hoverinfo="text",
    ))
    fig_cov.update_layout(
        title="Coverage Distribution",
        xaxis_title="Coverage (log10 x)",
        yaxis_title="Count",
        template="plotly_dark",
    )
    mo.ui.plotly(fig_cov)
    return


if __name__ == "__main__":
    app.run()
