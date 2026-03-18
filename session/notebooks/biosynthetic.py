import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="Biosynthetic Gene Clusters")


@app.cell
def _():
    import marimo as mo
    import plotly.graph_objects as go
    import pandas as pd
    from data_utils import load_viz_json
    return go, mo, pd, load_viz_json


@app.cell
def _(mo, load_viz_json):
    data = load_viz_json("biosynthetic")
    if not data:
        mo.stop(True, mo.md("**No biosynthetic data available.** antiSMASH may not have been run."))
    mo.md(f"# Biosynthetic Gene Clusters\n\n{data.get('n_regions', 0)} BGC regions detected")
    return (data,)


@app.cell
def _(mo, go, data):
    type_counts = data.get("type_counts", {})
    if not type_counts:
        mo.stop(True, mo.md("*No BGC types detected.*"))

    sorted_t = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
    fig = go.Figure(go.Bar(
        x=[t[0] for t in sorted_t],
        y=[t[1] for t in sorted_t],
        marker_color="#8b5cf6",
    ))
    fig.update_layout(
        title="BGC Type Distribution",
        xaxis_title="Product Type",
        yaxis_title="Count",
        template="plotly_dark",
        xaxis_tickangle=-45,
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, pd, data):
    regions = data.get("regions", [])
    if not regions:
        mo.stop(True, mo.md("*No BGC regions.*"))

    rows = []
    for r in regions:
        best_match = ""
        similarity = ""
        if r.get("known_matches"):
            best_match = r["known_matches"][0].get("description", "")
            similarity = f"{r['known_matches'][0].get('similarity', 0)*100:.0f}%"
        rows.append({
            "Contig": r["contig"],
            "Products": ", ".join(r.get("products", [])),
            "Length": f"{r.get('length', 0):,} bp",
            "MAG": r.get("mag", ""),
            "Best Match": best_match,
            "Similarity": similarity,
        })
    df = pd.DataFrame(rows)
    mo.vstack([
        mo.md(f"### BGC Regions ({len(regions)})"),
        mo.ui.table(df),
    ])
    return


@app.cell
def _(mo, pd, data):
    per_bin = data.get("per_bin", {})
    if not per_bin:
        mo.stop(True, mo.md("*No per-bin BGC data.*"))

    rows = []
    for mag, info in sorted(per_bin.items(), key=lambda x: x[1]["total"], reverse=True):
        rows.append({
            "MAG": mag,
            "Total BGCs": info["total"],
            "Types": ", ".join(f"{k}({v})" for k, v in info.get("types", {}).items()),
        })
    df = pd.DataFrame(rows)
    mo.vstack([
        mo.md("### BGCs Per MAG"),
        mo.ui.table(df),
    ])
    return


if __name__ == "__main__":
    app.run()
