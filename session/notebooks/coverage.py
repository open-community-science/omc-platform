import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="Coverage")


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
    data = load_viz_json("coverage")
    if not data:
        mo.stop(True, mo.md("**No coverage data available.**"))
    mo.md(f"# Coverage Across Samples\n\n{len(data['bins'])} bins, {len(data['sample_names'])} samples")
    return (data,)


@app.cell
def _(mo, go, np, data):
    # Coverage heatmap
    bins = data["bins"]
    samples = data["sample_names"]
    matrix = data["matrix"]

    # Log transform for better visualization
    log_matrix = [[np.log10(v + 0.01) for v in row] for row in matrix]

    fig = go.Figure(go.Heatmap(
        z=log_matrix,
        x=samples,
        y=[b["id"] for b in bins],
        colorscale="Viridis",
        colorbar=dict(title="log10(depth)"),
        hovertemplate="Bin: %{y}<br>Sample: %{x}<br>Depth: %{customdata:.1f}x<extra></extra>",
        customdata=matrix,
    ))
    fig.update_layout(
        title="Coverage Heatmap (log10 scale)",
        xaxis_title="Sample",
        yaxis_title="Bin",
        template="plotly_dark",
        height=max(400, len(bins) * 15 + 100),
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, go, data):
    # Depth distribution box plots per sample
    samples = data["sample_names"]
    matrix = data["matrix"]

    fig = go.Figure()
    for i, sample in enumerate(samples):
        depths = [row[i] for row in matrix if row[i] > 0]
        if depths:
            fig.add_trace(go.Box(y=depths, name=sample, marker_color=f"hsl({i*360//len(samples)}, 70%, 50%)"))

    fig.update_layout(
        title="Depth Distribution Per Sample",
        yaxis_title="Depth (x)",
        yaxis_type="log",
        template="plotly_dark",
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, go, data):
    # Total depth per bin
    bins = data["bins"]
    matrix = data["matrix"]
    totals = [(b["id"], sum(row)) for b, row in zip(bins, matrix)]
    totals.sort(key=lambda x: x[1], reverse=True)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[t[0] for t in totals[:30]],
        y=[t[1] for t in totals[:30]],
        marker_color="#3b82f6",
    ))
    fig.update_layout(
        title="Total Depth Per Bin (top 30)",
        xaxis_title="Bin",
        yaxis_title="Total Depth",
        template="plotly_dark",
        xaxis_tickangle=-45,
    )
    mo.ui.plotly(fig)
    return


if __name__ == "__main__":
    app.run()
