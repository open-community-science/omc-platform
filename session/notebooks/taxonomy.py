import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="Taxonomy")


@app.cell
def _():
    import marimo as mo
    import plotly.graph_objects as go
    import pandas as pd
    from data_utils import load_viz_json, fmt_bp
    return go, mo, pd, load_viz_json, fmt_bp


@app.cell
def _(mo, load_viz_json):
    data = load_viz_json("taxonomy_sunburst")
    if not data:
        mo.stop(True, mo.md("**No taxonomy data available.**"))
    sources = list(data.get("sunbursts", {}).keys())
    source_select = mo.ui.dropdown(options=sources, value=sources[0] if sources else None, label="Classifier")
    mo.md("# Community Taxonomy")
    return data, source_select, sources


@app.cell
def _(mo, source_select):
    source_select
    return


@app.cell
def _(mo, go, data, source_select):
    source = source_select.value
    if not source or source not in data.get("sunbursts", {}):
        mo.stop(True, mo.md("*Select a classifier source.*"))

    tree = data["sunbursts"][source]

    # Flatten d3 hierarchy into sunburst arrays
    ids, labels, parents, values = [], [], [], []

    def _walk(node, parent_id=""):
        node_id = f"{parent_id}/{node['name']}" if parent_id else node["name"]
        ids.append(node_id)
        labels.append(node["name"])
        parents.append(parent_id)
        values.append(node.get("value", 0))
        for child in node.get("children", []):
            _walk(child, node_id)

    _walk(tree)

    fig = go.Figure(go.Sunburst(
        ids=ids,
        labels=labels,
        parents=parents,
        values=values,
        branchvalues="total",
        hovertemplate="<b>%{label}</b><br>%{value:,} bp<br>%{percentParent:.1%} of parent<extra></extra>",
        maxdepth=4,
    ))
    fig.update_layout(
        title=f"Community Composition ({source})",
        template="plotly_dark",
        height=600,
    )
    mo.ui.plotly(fig)
    return


@app.cell
def _(mo, go, data, source_select, fmt_bp):
    # Per-rank bar chart using composition data
    source = source_select.value
    comp = data.get("composition", {})
    # Use dastool if available, otherwise first binner
    binner = "dastool" if "dastool" in comp else (list(comp.keys())[0] if comp else None)
    if not binner or source not in comp.get(binner, {}):
        mo.stop(True, mo.md("*No per-bin composition data.*"))

    rank_data = comp[binner][source]
    ranks = [r for r in ["phylum", "class", "order", "family", "genus"] if r in rank_data]
    if not ranks:
        mo.stop(True, mo.md("*No rank data.*"))

    rank = ranks[0]  # default to phylum
    taxa = rank_data[rank]
    # Sort by total bp, take top 15
    sorted_taxa = sorted(taxa.items(), key=lambda x: x[1], reverse=True)
    top = sorted_taxa[:15]
    if len(sorted_taxa) > 15:
        other_bp = sum(v for _, v in sorted_taxa[15:])
        top.append(("Other", other_bp))

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[t[0] for t in top],
        y=[t[1] / 1e6 for t in top],
        marker_color=["#ef4444", "#3b82f6", "#10b981", "#f59e0b", "#8b5cf6",
                       "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#6366f1",
                       "#14b8a6", "#a855f7", "#e11d48", "#0ea5e9", "#65a30d", "#94a3b8"][:len(top)],
    ))
    fig.update_layout(
        title=f"Top Taxa at {rank.title()} Rank ({source}, {binner})",
        xaxis_title=rank.title(),
        yaxis_title="Total Length (Mbp)",
        template="plotly_dark",
        xaxis_tickangle=-45,
    )
    mo.ui.plotly(fig)
    return


if __name__ == "__main__":
    app.run()
