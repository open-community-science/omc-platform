"""Fetch and parse SRA metadata from NCBI."""
from Bio import Entrez
from xml.etree import ElementTree
import json
from typing import Optional


# NCBI requires an email for Entrez queries
Entrez.email = "omc@opencommunity.science"


async def fetch_sra_metadata(accession: str) -> dict:
    """
    Fetch full metadata for an SRA accession from NCBI.

    Supports: SRR/ERR/DRR (run), SRX (experiment), SRP (study), PRJNA (BioProject)
    Returns parsed metadata dict.
    """
    accession = accession.strip().upper()

    # Determine which database to query
    if accession.startswith("PRJNA"):
        return await _fetch_bioproject(accession)
    else:
        return await _fetch_sra(accession)


async def _fetch_sra(accession: str) -> dict:
    """Fetch metadata from SRA database."""
    try:
        # Search for the accession
        search_handle = Entrez.esearch(db="sra", term=accession)
        search_results = Entrez.read(search_handle)
        search_handle.close()

        if not search_results["IdList"]:
            return {"error": f"Accession {accession} not found in SRA", "accession": accession}

        sra_id = search_results["IdList"][0]

        # Fetch full record
        fetch_handle = Entrez.efetch(db="sra", id=sra_id, rettype="full", retmode="xml")
        xml_data = fetch_handle.read()
        fetch_handle.close()

        return _parse_sra_xml(xml_data, accession)

    except Exception as e:
        return {"error": str(e), "accession": accession}


async def _fetch_bioproject(accession: str) -> dict:
    """Fetch metadata from BioProject database."""
    try:
        # Get BioProject summary
        search_handle = Entrez.esearch(db="bioproject", term=accession)
        search_results = Entrez.read(search_handle)
        search_handle.close()

        if not search_results["IdList"]:
            return {"error": f"BioProject {accession} not found", "accession": accession}

        bp_id = search_results["IdList"][0]

        summary_handle = Entrez.esummary(db="bioproject", id=bp_id)
        summary = Entrez.read(summary_handle)
        summary_handle.close()

        project_data = summary.get("DocumentSummarySet", {}).get("DocumentSummary", [{}])[0]

        # Find linked SRA runs via esearch (more reliable than elink)
        sra_search = Entrez.esearch(db="sra", term=accession, retmax=200)
        sra_results = Entrez.read(sra_search)
        sra_search.close()

        sra_ids = sra_results.get("IdList", [])
        total_runs = int(sra_results.get("Count", 0))

        metadata = {
            "accession": accession,
            "type": "BioProject",
            "title": project_data.get("Project_Title", ""),
            "description": project_data.get("Project_Description", ""),
            "organism": project_data.get("Organism_Name", ""),
            "organization": project_data.get("Submitter_Organization", ""),
            "registration_date": project_data.get("Registration_Date", ""),
            "project_scope": project_data.get("Project_Target_Scope", ""),
            "num_sra_runs": total_runs,
        }

        # Fetch metadata for the first SRA run as a representative sample
        if sra_ids:
            first_run_handle = Entrez.efetch(
                db="sra", id=sra_ids[0], rettype="full", retmode="xml"
            )
            first_xml = first_run_handle.read()
            first_run_handle.close()
            sample_metadata = _parse_sra_xml(first_xml, accession)

            # Merge useful fields from the sample run into the top-level
            for field in ["platform", "instrument_model", "library_strategy",
                          "library_source", "library_layout", "study_title"]:
                if field in sample_metadata and field not in metadata:
                    metadata[field] = sample_metadata[field]

            # Use organism from SRA if BioProject didn't have it
            if not metadata["organism"] and sample_metadata.get("organism"):
                metadata["organism"] = sample_metadata["organism"]

            # Include sample attributes from the representative run
            if "sample_attributes" in sample_metadata:
                metadata["sample_attributes"] = sample_metadata["sample_attributes"]

            metadata["sample_run_metadata"] = sample_metadata

        return metadata

    except Exception as e:
        return {"error": str(e), "accession": accession}


def _parse_sra_xml(xml_data: str | bytes, accession: str) -> dict:
    """Parse SRA XML response into a clean metadata dict."""
    if isinstance(xml_data, str):
        xml_data = xml_data.encode()

    root = ElementTree.fromstring(xml_data)

    metadata = {
        "accession": accession,
        "type": "SRA",
        "raw_xml_length": len(xml_data),
    }

    # Parse experiment package
    for exp_pkg in root.iter("EXPERIMENT_PACKAGE"):
        # Experiment info
        exp = exp_pkg.find(".//EXPERIMENT")
        if exp is not None:
            metadata["experiment_accession"] = exp.get("accession", "")
            title_el = exp.find(".//TITLE")
            if title_el is not None:
                metadata["experiment_title"] = title_el.text

            # Platform info
            platform = exp.find(".//PLATFORM")
            if platform is not None:
                for child in platform:
                    metadata["platform"] = child.tag
                    model_el = child.find("INSTRUMENT_MODEL")
                    if model_el is not None:
                        metadata["instrument_model"] = model_el.text

            # Library info
            lib = exp.find(".//LIBRARY_DESCRIPTOR")
            if lib is not None:
                for field in ["LIBRARY_NAME", "LIBRARY_STRATEGY", "LIBRARY_SOURCE",
                              "LIBRARY_SELECTION", "LIBRARY_LAYOUT"]:
                    el = lib.find(f".//{field}")
                    if el is not None:
                        if len(el) > 0:  # Has children (e.g., LIBRARY_LAYOUT/PAIRED)
                            metadata[field.lower()] = list(el)[0].tag
                        else:
                            metadata[field.lower()] = el.text

        # Study info
        study = exp_pkg.find(".//STUDY")
        if study is not None:
            metadata["study_accession"] = study.get("accession", "")
            desc = study.find(".//STUDY_DESCRIPTION")
            if desc is not None:
                metadata["study_description"] = desc.text
            title = study.find(".//STUDY_TITLE")
            if title is not None:
                metadata["study_title"] = title.text

        # Sample info
        sample = exp_pkg.find(".//SAMPLE")
        if sample is not None:
            metadata["sample_accession"] = sample.get("accession", "")
            title = sample.find(".//TITLE")
            if title is not None:
                metadata["sample_title"] = title.text
            taxon = sample.find(".//TAXON_ID")
            if taxon is not None:
                metadata["taxon_id"] = taxon.text
            sci_name = sample.find(".//SCIENTIFIC_NAME")
            if sci_name is not None:
                metadata["organism"] = sci_name.text

            # Sample attributes (the gold mine)
            attrs = {}
            for attr in sample.iter("SAMPLE_ATTRIBUTE"):
                tag_el = attr.find("TAG")
                val_el = attr.find("VALUE")
                if tag_el is not None and val_el is not None:
                    attrs[tag_el.text] = val_el.text
            metadata["sample_attributes"] = attrs

        # Run info
        run = exp_pkg.find(".//RUN")
        if run is not None:
            metadata["run_accession"] = run.get("accession", "")
            metadata["total_spots"] = run.get("total_spots", "")
            metadata["total_bases"] = run.get("total_bases", "")
            metadata["size_mb"] = round(int(run.get("size", "0")) / 1_000_000, 1)

        # Submission info
        submission = exp_pkg.find(".//SUBMISSION")
        if submission is not None:
            metadata["submission_accession"] = submission.get("accession", "")
            metadata["submission_center"] = submission.get("center_name", "")

        # Organization
        org = exp_pkg.find(".//Organization")
        if org is not None:
            name_el = org.find("Name")
            if name_el is not None:
                metadata["organization"] = name_el.text

        # Only parse first experiment package
        break

    return metadata


def metadata_summary(metadata: dict) -> str:
    """Generate a human-readable summary of the metadata for display."""
    if "error" in metadata:
        return f"Error fetching metadata: {metadata['error']}"

    lines = []
    lines.append(f"Accession: {metadata.get('accession', 'Unknown')}")

    if metadata.get("study_title"):
        lines.append(f"Study: {metadata['study_title']}")
    if metadata.get("organism"):
        lines.append(f"Organism: {metadata['organism']}")
    if metadata.get("platform"):
        lines.append(f"Platform: {metadata['platform']} ({metadata.get('instrument_model', '')})")
    if metadata.get("library_strategy"):
        lines.append(f"Library: {metadata['library_strategy']} / {metadata.get('library_source', '')} / {metadata.get('library_layout', '')}")
    if metadata.get("total_bases"):
        gb = round(int(metadata["total_bases"]) / 1e9, 2)
        lines.append(f"Data: {metadata.get('total_spots', '')} reads, {gb} Gbp")
    if metadata.get("organization"):
        lines.append(f"Center: {metadata['organization']}")

    attrs = metadata.get("sample_attributes", {})
    if attrs:
        lines.append("Sample attributes:")
        for k, v in attrs.items():
            lines.append(f"  {k}: {v}")

    return "\n".join(lines)
