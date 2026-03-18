import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="Phylogenetic Classification")


@app.cell
def _():
    import marimo as mo
    import plotly.graph_objects as go
    import pandas as pd
    from data_utils import load_viz_json
    return go, mo, pd, load_viz_json


@app.cell
def _(mo, load_viz_json):
    data = load_viz_json("phylotree")
    if not data or not data.get("bins"):
        mo.stop(True, mo.md("**No GTDB-Tk phylogenetic data available.**"))
    mo.md(f"# GTDB-Tk Phylogenetic Classification\n\n{len(data['bins'])} bins classified")
    return (data,)


@app.cell
def _(mo, pd, data):
    rows = []
    for b in data["bins"]:
        lin = b.get("lineage", {})
        rows.append({
            "Bin": b["name"],
            "Binner": b.get("binner", ""),
            "Method": b.get("method", ""),
            "ANI%": f"{b['ani']:.1f}" if b.get("ani") else "",
            "Domain": lin.get("domain", ""),
            "Phylum": lin.get("phylum", ""),
            "Class": lin.get("class", ""),
            "Order": lin.get("order", ""),
            "Family": lin.get("family", ""),
            "Genus": lin.get("genus", ""),
            "Species": lin.get("species", ""),
        })
    df = pd.DataFrame(rows)
    mo.vstack([
        mo.md("### GTDB Classification Table"),
        mo.ui.table(df),
    ])
    return (df,)


@app.cell
def _(mo, go, data):
    # Phylum distribution bar chart
    phyla = {}
    for b in data["bins"]:
        p = b.get("lineage", {}).get("phylum", "Unclassified") or "Unclassified"
        phyla[p] = phyla.get(p, 0) + 1

    sorted_p = sorted(phyla.items(), key=lambda x: x[1], reverse=True)
    palette = ["#ef4444", "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6",
               "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#6366f1"]

    fig = go.Figure(go.Bar(
        x=[p[0] for p in sorted_p],
        y=[p[1] for p in sorted_p],
        marker_color=[palette[i % len(palette)] for i in range(len(sorted_p))],
    ))
    fig.update_layout(
        title="Bins by Phylum",
        xaxis_title="Phylum",
        yaxis_title="Count",
        template="plotly_dark",
        xaxis_tickangle=-45,
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, go, data):
    # Classification method breakdown
    methods = {}
    for b in data["bins"]:
        m = b.get("method", "unknown")
        methods[m] = methods.get(m, 0) + 1

    if not methods:
        mo.stop(True, mo.md("*No method data.*"))

    fig = go.Figure(go.Pie(
        labels=list(methods.keys()),
        values=list(methods.values()),
        hole=0.4,
    ))
    fig.update_layout(title="Classification Method", template="plotly_dark")
    mo.ui.plotly(fig)
    return


if __name__ == "__main__":
    app.run()
