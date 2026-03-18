import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="Eukaryotic Detection")


@app.cell
def _():
    import marimo as mo
    import plotly.graph_objects as go
    import pandas as pd
    from data_utils import load_viz_json
    return go, mo, pd, load_viz_json


@app.cell
def _(mo, load_viz_json):
    data = load_viz_json("eukaryotic")
    if not data:
        mo.stop(True, mo.md("**No eukaryotic detection data available.**"))
    mo.md("# Eukaryotic Detection")
    return (data,)


@app.cell
def _(mo, go, data):
    # Tiara classification donut
    tiara = data.get("tiara_size_weighted", data.get("tiara_counts", {}))
    if not tiara:
        mo.stop(True, mo.md("*No Tiara classification data.*"))

    color_map = {
        "bacteria": "#06b6d4", "archaea": "#f59e0b", "eukaryota": "#10b981",
        "organelle": "#8b5cf6", "unknown": "#64748b",
    }
    labels = list(tiara.keys())
    values = list(tiara.values())
    colors = [color_map.get(l.lower(), "#94a3b8") for l in labels]

    fig = go.Figure(go.Pie(
        labels=labels, values=values, hole=0.5,
        marker_colors=colors,
        textinfo="label+percent",
    ))
    fig.update_layout(title="Domain Classification (Tiara, size-weighted)", template="plotly_dark")
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, go, data):
    # Whokaryote classification
    whokaryote = data.get("whokaryote_counts", {})
    if not whokaryote:
        mo.stop(True, mo.md("*No Whokaryote data.*"))

    fig = go.Figure(go.Bar(
        x=list(whokaryote.keys()),
        y=list(whokaryote.values()),
        marker_color="#8b5cf6",
    ))
    fig.update_layout(
        title="Whokaryote Classification",
        xaxis_title="Classification",
        yaxis_title="Count",
        template="plotly_dark",
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, go, data):
    # MarFERReT eukaryotic phyla
    marferret = data.get("marferret_taxonomy", {})
    if not marferret:
        mo.stop(True, mo.md("*No MarFERReT data.*"))

    sorted_m = sorted(marferret.items(), key=lambda x: x[1], reverse=True)[:15]
    palette = ["#ef4444", "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6",
               "#ec4899", "#06b6d4", "#84cc16"]

    fig = go.Figure(go.Bar(
        x=[m[0] for m in sorted_m],
        y=[m[1] for m in sorted_m],
        marker_color=[palette[i % len(palette)] for i in range(len(sorted_m))],
    ))
    fig.update_layout(
        title="MarFERReT Eukaryotic Phyla (top 15)",
        xaxis_title="Phylum",
        yaxis_title="Contig Count",
        template="plotly_dark",
        xaxis_tickangle=-45,
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, pd, data):
    contigs = data.get("marferret_contigs", [])
    if not contigs:
        mo.stop(True, mo.md("*No MarFERReT contig data.*"))

    df = pd.DataFrame(contigs[:200])
    mo.vstack([
        mo.md(f"### Eukaryotic Contigs ({len(contigs)} total, showing 200)"),
        mo.ui.table(df),
    ])
    return


if __name__ == "__main__":
    app.run()
