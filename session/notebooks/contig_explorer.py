import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="Contig Explorer")


@app.cell
def _():
    import marimo as mo
    import plotly.graph_objects as go
    import pandas as pd
    import numpy as np
    from data_utils import load_viz_json
    return go, mo, np, pd, load_viz_json


@app.cell
def _(mo, load_viz_json):
    data = load_viz_json("contig_explorer")
    if not data or not data.get("contigs"):
        mo.stop(True, mo.md("**No contig explorer data available.**"))

    projections = ["pca"]
    if data.get("has_tsne"):
        projections.append("tsne")
    if data.get("has_umap"):
        projections.append("umap")

    projection_select = mo.ui.dropdown(options=projections, value="pca", label="Projection")
    color_select = mo.ui.dropdown(
        options=["bin", "gc", "depth", "length", "kaiju_phylum", "kraken2_phylum", "replicon"],
        value="bin",
        label="Color by",
    )
    mo.md(f"# Contig Explorer\n\n{len(data['contigs']):,} contigs")
    return data, projection_select, color_select, projections


@app.cell
def _(mo, projection_select, color_select):
    mo.hstack([projection_select, color_select])
    return


@app.cell
def _(mo, go, np, data, projection_select, color_select):
    contigs = data["contigs"]
    proj = projection_select.value
    color_by = color_select.value

    # Get x/y coordinates
    if proj == "pca":
        x = [c.get("pca_x", 0) for c in contigs]
        y = [c.get("pca_y", 0) for c in contigs]
    elif proj == "tsne":
        x = [c.get("tsne_x", 0) for c in contigs]
        y = [c.get("tsne_y", 0) for c in contigs]
    else:
        x = [c.get("umap_x", 0) for c in contigs]
        y = [c.get("umap_y", 0) for c in contigs]

    # Color logic
    if color_by in ("gc", "depth", "length"):
        vals = [c.get(color_by, 0) for c in contigs]
        if color_by in ("depth", "length"):
            vals = [np.log10(max(v, 0.01)) for v in vals]
        fig = go.Figure(go.Scattergl(
            x=x, y=y, mode="markers",
            marker=dict(size=3, color=vals, colorscale="Viridis",
                        colorbar=dict(title=f"{'log10 ' if color_by in ('depth','length') else ''}{color_by}")),
            hovertext=[c["id"] for c in contigs],
            hoverinfo="text",
        ))
    else:
        # Categorical coloring
        categories = {}
        for c in contigs:
            cat = c.get(color_by, "") or "unassigned"
            categories.setdefault(cat, [])
            categories[cat].append(c)

        # Sort by size, limit to top 20
        sorted_cats = sorted(categories.items(), key=lambda x: len(x[1]), reverse=True)
        palette = ["#ef4444", "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6",
                    "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#6366f1",
                    "#14b8a6", "#a855f7", "#e11d48", "#0ea5e9", "#65a30d",
                    "#d946ef", "#fb923c", "#22d3ee", "#a3e635", "#f472b6"]

        fig = go.Figure()
        for i, (cat, cat_contigs) in enumerate(sorted_cats[:20]):
            cx = []
            cy = []
            for c in cat_contigs:
                if proj == "pca":
                    cx.append(c.get("pca_x", 0)); cy.append(c.get("pca_y", 0))
                elif proj == "tsne":
                    cx.append(c.get("tsne_x", 0)); cy.append(c.get("tsne_y", 0))
                else:
                    cx.append(c.get("umap_x", 0)); cy.append(c.get("umap_y", 0))
            fig.add_trace(go.Scattergl(
                x=cx, y=cy, mode="markers", name=cat[:30],
                marker=dict(size=3, color=palette[i % len(palette)]),
                hovertext=[c["id"] for c in cat_contigs],
                hoverinfo="text",
            ))
        # Remaining as gray
        if len(sorted_cats) > 20:
            rest = [c for _, cs in sorted_cats[20:] for c in cs]
            rx, ry = [], []
            for c in rest:
                if proj == "pca":
                    rx.append(c.get("pca_x", 0)); ry.append(c.get("pca_y", 0))
                elif proj == "tsne":
                    rx.append(c.get("tsne_x", 0)); ry.append(c.get("tsne_y", 0))
                else:
                    rx.append(c.get("umap_x", 0)); ry.append(c.get("umap_y", 0))
            fig.add_trace(go.Scattergl(
                x=rx, y=ry, mode="markers", name="other",
                marker=dict(size=2, color="#475569"),
            ))

    fig.update_layout(
        title=f"Contig Space ({proj.upper()}, colored by {color_by})",
        template="plotly_dark",
        height=600,
        xaxis_title=f"{proj.upper()} 1",
        yaxis_title=f"{proj.upper()} 2",
    )
    mo.ui.plotly(fig)
    return


if __name__ == "__main__":
    app.run()
