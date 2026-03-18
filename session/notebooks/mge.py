import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="Mobile Genetic Elements")


@app.cell
def _():
    import marimo as mo
    import plotly.graph_objects as go
    import pandas as pd
    from data_utils import load_viz_json
    return go, mo, pd, load_viz_json


@app.cell
def _(mo, load_viz_json):
    data = load_viz_json("mge_summary")
    if not data:
        mo.stop(True, mo.md("**No MGE data available.**"))

    mo.md(f"""# Mobile Genetic Elements

| Category | Count |
|----------|-------|
| Viral contigs | {data.get('n_virus', 0):,} |
| Plasmid contigs | {data.get('n_plasmid', 0):,} |
| Defense systems | {data.get('n_defense', 0):,} |
| Integron elements | {data.get('n_integron_elements', 0):,} |
""")
    return (data,)


@app.cell
def _(mo, go, data):
    # Defense system type distribution
    defense = data.get("defense_types", {})
    if not defense:
        mo.stop(True, mo.md("*No defense systems detected.*"))

    sorted_d = sorted(defense.items(), key=lambda x: x[1], reverse=True)
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[d[0] for d in sorted_d],
        y=[d[1] for d in sorted_d],
        marker_color="#06b6d4",
    ))
    fig.update_layout(
        title="Defense System Types",
        xaxis_title="Type",
        yaxis_title="Count",
        template="plotly_dark",
        xaxis_tickangle=-45,
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, go, data):
    # Viral taxonomy pie chart
    viral_tax = data.get("viral_taxonomy", {})
    if not viral_tax:
        mo.stop(True, mo.md("*No viral taxonomy data.*"))

    sorted_v = sorted(viral_tax.items(), key=lambda x: x[1], reverse=True)
    fig = go.Figure()
    fig.add_trace(go.Pie(
        labels=[v[0] for v in sorted_v],
        values=[v[1] for v in sorted_v],
        hole=0.5,
        textinfo="label+percent",
    ))
    fig.update_layout(
        title="Viral Taxonomy (geNomad)",
        template="plotly_dark",
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, pd, load_viz_json):
    per_bin = load_viz_json("mge_per_bin")
    if not per_bin:
        mo.stop(True, mo.md("*No per-bin MGE data.*"))

    rows = []
    for mag, mge in per_bin.items():
        rows.append({
            "MAG": mag,
            "Viruses": len(mge.get("viruses", [])),
            "Plasmids": len(mge.get("plasmids", [])),
            "Defense": len(mge.get("defense", [])),
            "Integrons": len(mge.get("integrons", [])),
        })
    df = pd.DataFrame(rows).sort_values("Viruses", ascending=False)
    mo.vstack([
        mo.md("### Per-MAG MGE Summary"),
        mo.ui.table(df),
    ])
    return


if __name__ == "__main__":
    app.run()
