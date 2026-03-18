import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="KEGG Metabolism")


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
    data = load_viz_json("kegg_heatmap")
    if not data:
        mo.stop(True, mo.md("**No KEGG metabolism data available.**"))
    mo.md(f"# KEGG Module Completeness\n\n{len(data['mag_ids'])} MAGs, {len(data['module_ids'])} modules")
    return (data,)


@app.cell
def _(mo, go, np, data):
    mag_ids = data["mag_ids"]
    module_ids = data["module_ids"]
    module_names = data["module_names"]
    groups = data["module_groups"]
    matrix = data["matrix"]

    fig = go.Figure(go.Heatmap(
        z=matrix,
        x=[f"{mid} {mname[:30]}" for mid, mname in zip(module_ids, module_names)],
        y=mag_ids,
        colorscale="Viridis",
        zmin=0, zmax=1,
        colorbar=dict(title="Completeness"),
        hovertemplate="MAG: %{y}<br>Module: %{x}<br>Completeness: %{z:.0%}<extra></extra>",
    ))
    fig.update_layout(
        title="KEGG Module Completeness",
        template="plotly_dark",
        height=max(400, len(mag_ids) * 20 + 150),
        xaxis_tickangle=-45,
        xaxis_tickfont_size=8,
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, go, data):
    # Module category summary
    groups = data["module_groups"]
    matrix = data["matrix"]
    module_names = data["module_names"]

    # Average completeness by group
    group_set = sorted(set(groups))
    group_avg = {}
    group_detected = {}
    for g in group_set:
        indices = [i for i, gr in enumerate(groups) if gr == g]
        if indices:
            vals = [matrix[r][c] for r in range(len(matrix)) for c in indices]
            group_avg[g] = sum(vals) / len(vals) if vals else 0
            group_detected[g] = sum(1 for v in vals if v >= 0.75)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=list(group_avg.keys()),
        y=[v * 100 for v in group_avg.values()],
        marker_color=["#ef4444", "#3b82f6", "#f59e0b", "#10b981", "#8b5cf6", "#94a3b8"][:len(group_avg)],
        text=[f"{v*100:.0f}%" for v in group_avg.values()],
        textposition="outside",
    ))
    fig.update_layout(
        title="Average Module Completeness by Category",
        xaxis_title="Category",
        yaxis_title="Mean Completeness (%)",
        yaxis_range=[0, 100],
        template="plotly_dark",
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, pd, data):
    rows = []
    for i, (mid, mname, group, cat) in enumerate(zip(
        data["module_ids"], data["module_names"], data["module_groups"], data["module_categories"]
    )):
        avg = sum(data["matrix"][r][i] for r in range(len(data["matrix"]))) / max(len(data["matrix"]), 1)
        detected = sum(1 for r in range(len(data["matrix"])) if data["matrix"][r][i] >= 0.75)
        rows.append({
            "Module": mid,
            "Name": mname,
            "Group": group,
            "Category": cat,
            "Avg Completeness": f"{avg*100:.0f}%",
            "Detected (≥75%)": detected,
        })
    df = pd.DataFrame(rows).sort_values("Avg Completeness", ascending=False)
    mo.vstack([
        mo.md("### Module Details"),
        mo.ui.table(df),
    ])
    return


if __name__ == "__main__":
    app.run()
