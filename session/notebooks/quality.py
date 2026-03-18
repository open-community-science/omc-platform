import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="MAG Quality")


@app.cell
def _():
    import marimo as mo
    import plotly.graph_objects as go
    import pandas as pd
    from data_utils import load_viz_json, fmt_bp
    return go, mo, pd, load_viz_json, fmt_bp


@app.cell
def _(mo, load_viz_json):
    data = load_viz_json("checkm2_all")
    if not data:
        mo.stop(True, mo.md("**No bin quality data available.** No MAGs were recovered."))
    mo.md(f"# MAG Quality Assessment\n\n{len(data)} bins from all binners")
    return (data,)


@app.cell
def _(mo, go, data):
    # Quality scatter: completeness vs contamination
    binner_symbols = {
        "dastool": "circle", "semibin": "square", "metabat": "diamond",
        "maxbin": "triangle-up", "lorbin": "cross", "comebin": "star",
        "vamb": "hexagon", "vamb_tax": "pentagon", "binette": "hourglass",
        "magscot": "bowtie",
    }
    binner_colors = {
        "dastool": "#ef4444", "semibin": "#3b82f6", "metabat": "#10b981",
        "maxbin": "#f59e0b", "lorbin": "#8b5cf6", "comebin": "#ec4899",
        "vamb": "#06b6d4", "vamb_tax": "#84cc16", "binette": "#f97316",
        "magscot": "#6366f1",
    }

    fig = go.Figure()
    # Group by binner
    binners = {}
    for b in data:
        binner = b.get("binner", "unknown")
        binners.setdefault(binner, []).append(b)

    for binner, bins in binners.items():
        fig.add_trace(go.Scatter(
            x=[b["contamination"] for b in bins],
            y=[b["completeness"] for b in bins],
            mode="markers",
            name=binner,
            marker=dict(
                size=[max(4, (b.get("genome_size", 1e6) / 1e6) ** 0.5 * 3) for b in bins],
                symbol=binner_symbols.get(binner, "circle"),
                color=binner_colors.get(binner, "#94a3b8"),
            ),
            text=[b["name"] for b in bins],
            hovertemplate="%{text}<br>Comp: %{y:.1f}%<br>Cont: %{x:.1f}%<extra>" + binner + "</extra>",
        ))

    # MIMAG threshold lines
    fig.add_shape(type="line", x0=0, x1=5, y0=90, y1=90, line=dict(dash="dash", color="#10b981", width=1))
    fig.add_shape(type="line", x0=5, x1=5, y0=90, y1=100, line=dict(dash="dash", color="#10b981", width=1))
    fig.add_shape(type="line", x0=0, x1=10, y0=50, y1=50, line=dict(dash="dash", color="#f59e0b", width=1))
    fig.add_shape(type="line", x0=10, x1=10, y0=50, y1=100, line=dict(dash="dash", color="#f59e0b", width=1))
    fig.add_annotation(x=2.5, y=95, text="HQ", showarrow=False, font=dict(color="#10b981"))
    fig.add_annotation(x=7, y=70, text="MQ", showarrow=False, font=dict(color="#f59e0b"))

    fig.update_layout(
        title="Bin Quality: Completeness vs Contamination",
        xaxis_title="Contamination (%)",
        yaxis_title="Completeness (%)",
        template="plotly_dark",
        xaxis=dict(range=[-1, max(b["contamination"] for b in data) * 1.1 + 1]),
        yaxis=dict(range=[0, 105]),
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, pd, data, fmt_bp):
    df = pd.DataFrame([{
        "Name": b["name"],
        "Binner": b.get("binner", ""),
        "Completeness": round(b["completeness"], 1),
        "Contamination": round(b["contamination"], 1),
        "Size": fmt_bp(b.get("genome_size", 0)),
        "GC%": round(b.get("gc", 0), 1),
        "N50": f"{b.get('n50', 0):,}",
        "Contigs": b.get("n_contigs", 0),
        "DAS Tool": "yes" if b.get("is_dastool") else "",
    } for b in data])
    df = df.sort_values("Completeness", ascending=False)
    mo.vstack([
        mo.md("### Bin Quality Table"),
        mo.ui.table(df),
    ])
    return


if __name__ == "__main__":
    app.run()
