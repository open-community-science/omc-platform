import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="OMC Data Explorer")


@app.cell
def _():
    import marimo as mo
    import os
    import json
    from pathlib import Path
    import pandas as pd

    data_dir = Path(os.environ.get("DATA_DIR", "/data"))

    # Load submission metadata (round-trips with the data)
    _meta_file = data_dir / "metadata.json"
    if not _meta_file.exists():
        _meta_file = Path("/metadata/metadata.json")
    submission_meta = json.loads(_meta_file.read_text()) if _meta_file.exists() else {}

    return data_dir, json, mo, os, pd, Path, submission_meta


@app.cell
def _(mo):
    mo.md(
        """
        # Your Pipeline Results

        This interactive explorer lets you dig into your data.
        Use the controls below to filter and visualize your results.
        The AI assistant in the chat panel can help interpret what you see.
        """
    )
    return


@app.cell
def _(mo, pd, submission_meta):
    """Show sample metadata from ENA/NCBI if available."""
    _records = submission_meta.get("sample_metadata", {}).get("sample_records", [])
    if _records:
        _df = pd.DataFrame(_records)
        # Show key columns first
        _priority = ["run_accession", "sample_alias", "collection_date", "country",
                      "host", "scientific_name", "instrument_platform", "library_strategy",
                      "environment_biome", "lat", "lon", "base_count"]
        _cols = [c for c in _priority if c in _df.columns]
        _cols += [c for c in _df.columns if c not in _cols]
        _df = _df[_cols]
        mo.vstack([
            mo.md(f"### Sample Metadata ({len(_records)} runs)"),
            mo.ui.table(_df),
        ])
    return


@app.cell
def _(mo, data_dir):
    _available = {}
    for _subdir in ["assembly", "binning", "taxonomy", "annotation", "metabolism", "eukaryotic"]:
        _p = data_dir / _subdir
        if _p.exists():
            _files = [f.name for f in _p.iterdir() if f.is_file()]
            if _files:
                _available[_subdir] = _files

    available_categories = _available

    if not available_categories:
        mo.md("**No pipeline results found yet.** Check back after the pipeline completes.")
    else:
        mo.md(f"**Found results in {len(available_categories)} categories:** {', '.join(available_categories.keys())}")
    return (available_categories,)


@app.cell
def _(mo, available_categories):
    category = mo.ui.dropdown(
        options=list(available_categories.keys()) if available_categories else [],
        value=list(available_categories.keys())[0] if available_categories else None,
        label="Result category",
    )
    mo.hstack([category])
    return (category,)


@app.cell
def _(mo, category, available_categories, data_dir, pd):
    if available_categories and category.value:
        _cat_dir = data_dir / category.value
        _file_list = []
        for _f in sorted(_cat_dir.iterdir()):
            if _f.is_file():
                _size = _f.stat().st_size
                _size_str = f"{_size / 1024:.0f} KB" if _size < 1_000_000 else f"{_size / 1_000_000:.1f} MB"
                _file_list.append({"File": _f.name, "Size": _size_str})
        if _file_list:
            _df = pd.DataFrame(_file_list)
            mo.vstack([
                mo.md(f"### {category.value.title()} — {len(_file_list)} files"),
                mo.ui.table(_df),
            ])
    return


@app.cell
def _(mo, data_dir, pd):
    """MAG quality scatter plot if binning results exist."""
    _quality_files = list(data_dir.rglob("*quality*")) + list(data_dir.rglob("*checkm*"))

    _mag_output = None
    if _quality_files:
        try:
            _qf = _quality_files[0]
            if _qf.suffix in (".tsv", ".txt"):
                _qdf = pd.read_csv(_qf, sep="\t")
            elif _qf.suffix == ".csv":
                _qdf = pd.read_csv(_qf)
            else:
                _qdf = None

            if _qdf is not None:
                _comp_col = next((c for c in _qdf.columns if "complet" in c.lower()), None)
                _cont_col = next((c for c in _qdf.columns if "contam" in c.lower()), None)

                if _comp_col and _cont_col:
                    import plotly.express as _px

                    _fig = _px.scatter(
                        _qdf, x=_comp_col, y=_cont_col,
                        title="MAG Quality: Completeness vs Contamination",
                        labels={_comp_col: "Completeness (%)", _cont_col: "Contamination (%)"},
                    )
                    _fig.add_hline(y=5, line_dash="dash", line_color="red", annotation_text="5% contamination")
                    _fig.add_vline(x=50, line_dash="dash", line_color="orange", annotation_text="50% completeness")

                    _mag_output = mo.vstack([
                        mo.md("### MAG Quality Assessment"),
                        mo.ui.plotly(_fig),
                    ])
        except Exception as _e:
            _mag_output = mo.md(f"*Could not parse quality report: {_e}*")

    if _mag_output is not None:
        _mag_output
    return


@app.cell
def _(mo, data_dir, pd):
    """Taxonomy overview table if available."""
    _tax_files = list(data_dir.rglob("*taxonomy*")) + list(data_dir.rglob("*gtdb*")) + list(data_dir.rglob("*kraken*"))

    _tax_output = None
    if _tax_files:
        try:
            _tf = _tax_files[0]
            if _tf.suffix in (".tsv", ".txt"):
                _tdf = pd.read_csv(_tf, sep="\t")
            elif _tf.suffix == ".csv":
                _tdf = pd.read_csv(_tf)
            else:
                _tdf = None

            if _tdf is not None:
                _tax_output = mo.vstack([
                    mo.md(f"### Taxonomy Overview\n\nLoaded from `{_tf.name}` — {len(_tdf)} entries"),
                    mo.ui.table(_tdf.head(20)),
                ])
        except Exception as _e:
            _tax_output = mo.md(f"*Could not parse taxonomy file: {_e}*")

    if _tax_output is not None:
        _tax_output
    return


if __name__ == "__main__":
    app.run()
