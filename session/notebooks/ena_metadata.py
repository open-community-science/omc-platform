import marimo

__generated_with = "0.20.4"
app = marimo.App(width="full", app_title="ENA Metadata Editor")


@app.cell
def _():
    import marimo as mo
    import pandas as pd
    import json
    import os
    from pathlib import Path

    WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    # ENA checklists available for selection
    CHECKLISTS = {
        "ERC000011": "Default sample checklist",
        "ERC000024": "Water (GSC MIxS water)",
        "ERC000014": "Soil / Human associated (GSC MIxS)",
        "ERC000028": "Sediment (GSC MIxS sediment)",
        "ERC000013": "Host associated (GSC MIxS host)",
    }

    checklist_selector = mo.ui.dropdown(
        options=CHECKLISTS,
        value="ERC000024",
        label="ENA Checklist",
    )
    mo.md(f"""
    # ENA Sample Metadata Editor

    Select the appropriate checklist for your samples, then fill in the metadata table below.
    Required fields are pre-populated based on the checklist.

    **Checklist:** {checklist_selector}
    """)
    return CHECKLISTS, WORKSPACE, checklist_selector, json, mo, os, pd, Path


@app.cell
def _(checklist_selector, pd):
    """Generate template columns based on selected checklist."""
    # Inline checklist → required fields (avoids importing from ai/ inside container)
    CHECKLIST_REQUIRED = {
        "ERC000011": ["scientific_name", "collection date"],
        "ERC000024": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "depth",
        ],
        "ERC000014": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "depth", "elevation",
        ],
        "ERC000028": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "depth",
        ],
        "ERC000013": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "host scientific name",
        ],
    }

    OPTIONAL = [
        "sample_title", "sample_description", "isolation_source",
        "temperature", "pH", "salinity",
    ]

    selected = checklist_selector.value or "ERC000024"
    required = CHECKLIST_REQUIRED.get(selected, CHECKLIST_REQUIRED["ERC000011"])
    columns = ["sample_alias", "tax_id"] + required + [
        f for f in OPTIONAL if f not in required
    ]

    # Create empty template with one blank row for editing
    template_df = pd.DataFrame([{col: "" for col in columns}])
    return CHECKLIST_REQUIRED, OPTIONAL, columns, required, selected, template_df


@app.cell
def _(mo, template_df):
    """Editable sample metadata table."""
    mo.md("## Sample Metadata")
    sample_editor = mo.ui.dataframe(template_df)
    sample_editor
    return (sample_editor,)


@app.cell
def _(mo, sample_editor, selected):
    """Validate current metadata against checklist requirements."""
    import re as _re

    df = sample_editor.value
    errors = []
    warnings = []

    CHECKLIST_REQUIRED_VAL = {
        "ERC000011": ["scientific_name", "collection date"],
        "ERC000024": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "depth",
        ],
        "ERC000028": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "depth",
        ],
        "ERC000013": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "host scientific name",
        ],
    }
    req_fields = CHECKLIST_REQUIRED_VAL.get(selected, CHECKLIST_REQUIRED_VAL["ERC000011"])

    for idx, row in df.iterrows():
        alias = row.get("sample_alias", f"row {idx + 1}")
        if not str(alias).strip():
            alias = f"row {idx + 1}"
            warnings.append(f"Row {idx + 1}: no sample_alias set")
        for field in req_fields:
            val = row.get(field, "")
            if not str(val).strip():
                errors.append(f"**{alias}**: missing `{field}`")
        tax = row.get("tax_id", "")
        if str(tax).strip():
            try:
                int(str(tax).strip())
            except ValueError:
                errors.append(f"**{alias}**: tax_id must be numeric")
        cdate = str(row.get("collection date", "")).strip()
        if cdate and not _re.match(r"^\d{4}(-\d{2}(-\d{2})?)?$", cdate):
            warnings.append(f"**{alias}**: collection date should be YYYY, YYYY-MM, or YYYY-MM-DD")

    if errors:
        status = f"### Validation: {len(errors)} error(s)\n" + "\n".join(f"- {e}" for e in errors)
    else:
        status = "### Validation: All required fields present"
    if warnings:
        status += f"\n\n**Warnings:**\n" + "\n".join(f"- {w}" for w in warnings)
    mo.md(status)
    return (errors,)


@app.cell
def _(WORKSPACE, mo, pd, sample_editor):
    """Export sample metadata as TSV."""
    def _export_samples():
        df = sample_editor.value
        out_path = WORKSPACE / "samples.tsv"
        df.to_csv(out_path, sep="\t", index=False)
        return out_path

    export_btn = mo.ui.button(label="Export samples.tsv", on_click=lambda _: _export_samples())
    mo.md(f"""
    ## Export

    Save the current table to `/workspace/samples.tsv` for submission.

    {export_btn}
    """)
    return (export_btn,)


@app.cell
def _(mo, pd):
    """Experiment / library metadata editor."""
    mo.md("## Experiment Metadata")

    exp_columns = [
        "sample_alias", "library_strategy", "library_source",
        "library_selection", "platform", "instrument_model",
    ]
    exp_defaults = {
        "sample_alias": "",
        "library_strategy": "WGS",
        "library_source": "METAGENOMIC",
        "library_selection": "RANDOM",
        "platform": "OXFORD_NANOPORE",
        "instrument_model": "MinION",
    }
    exp_df = pd.DataFrame([exp_defaults])
    exp_editor = mo.ui.dataframe(exp_df)
    exp_editor
    return exp_columns, exp_defaults, exp_df, exp_editor


@app.cell
def _(mo, pd):
    """Run / file info editor."""
    mo.md("## Run Files")
    mo.md("Enter the filename, file type, and MD5 checksum for each file you will upload to ENA via FTP.")

    run_columns = ["sample_alias", "filename", "filetype", "md5_checksum"]
    run_df = pd.DataFrame([{c: "" for c in run_columns}])
    run_df["filetype"] = "fastq"
    run_editor = mo.ui.dataframe(run_df)
    run_editor
    return run_columns, run_df, run_editor


@app.cell
def _(mo, run_editor):
    """Generate FTP upload instructions."""
    df = run_editor.value
    files = [str(f) for f in df["filename"] if str(f).strip()]
    if not files:
        mo.md("*Add filenames above to generate FTP instructions.*")
    else:
        cmds = ["# Upload files to ENA via FTP", ""]
        cmds.append("# Option 1: lftp")
        cmds.append("lftp -u YOUR_WEBIN_ID webin2.ebi.ac.uk << 'EOF'")
        for f in files:
            cmds.append(f"put {f}")
        cmds.extend(["bye", "EOF", ""])
        cmds.append("# Option 2: curl (one file at a time)")
        for f in files:
            cmds.append(f"curl -T {f} ftp://webin2.ebi.ac.uk/ --user YOUR_WEBIN_ID")
        cmds.extend(["", "# Generate MD5 checksums"])
        for f in files:
            cmds.append(f"md5sum {f}")
        mo.md(f"### FTP Upload Instructions\n```bash\n{chr(10).join(cmds)}\n```")
    return ()


@app.cell
def _(WORKSPACE, mo):
    """Study registration fields."""
    mo.md("## Study Registration")

    study_alias = mo.ui.text(label="Study alias", placeholder="my-metagenome-study")
    study_title = mo.ui.text(label="Study title", placeholder="Metagenomic survey of ...")
    study_desc = mo.ui.text_area(label="Description", placeholder="Describe the study...")
    hold_date = mo.ui.text(label="Hold date (optional)", placeholder="YYYY-MM-DD")

    mo.md(f"""
    Fill in study details. These will be saved to `/workspace/study.json`.

    {study_alias}
    {study_title}
    {study_desc}
    {hold_date}
    """)
    return hold_date, study_alias, study_desc, study_title


@app.cell
def _(WORKSPACE, hold_date, json, mo, study_alias, study_desc, study_title):
    """Save study metadata to workspace."""
    def _save_study():
        data = {
            "alias": study_alias.value,
            "title": study_title.value,
            "description": study_desc.value,
            "hold_date": hold_date.value or None,
        }
        out = WORKSPACE / "study.json"
        out.write_text(json.dumps(data, indent=2))
        return out

    save_study_btn = mo.ui.button(label="Save study.json", on_click=lambda _: _save_study())
    save_study_btn
    return (save_study_btn,)


if __name__ == "__main__":
    app.run()
