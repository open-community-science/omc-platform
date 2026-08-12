"""Fetch and parse SRA metadata from NCBI and EBI/ENA."""
import csv
import io
import logging
from typing import Optional

import httpx
from Bio import Entrez
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

# NCBI requires an email for Entrez queries
Entrez.email = "omc@opencommunity.science"

# EBI/ENA portal API — returns per-run metadata as TSV, including MIxS fields
ENA_API = "https://www.ebi.ac.uk/ena/portal/api/filereport"

# Request all fields from ENA — let the data speak for itself.
# Empty columns are stripped on parsing.
ENA_FIELDS = "all"


async def resolve_to_bioproject(accession: str) -> dict:
    """
    Resolve any INSDC accession to its parent BioProject.

    Accepts SRR/ERR/DRR, SRX, SRP, SAMN/SAME/SAMD, or PRJNA.
    Always returns BioProject-level metadata with the resolved accession.
    Includes 'resolved_from' if the input was not already a BioProject.
    """
    accession = accession.strip().upper()

    # BioProject accessions (NCBI, EBI, DDBJ)
    if accession.startswith(("PRJNA", "PRJEB", "PRJDB")):
        return await _fetch_bioproject(accession)

    # SRA/ENA run, experiment, study, or EBI sample accessions
    if accession.startswith(("SRR", "ERR", "DRR", "SRX", "SRP", "ERS")):
        sra_meta = await _fetch_sra(accession)
        if "error" in sra_meta:
            return sra_meta

        bioproject_id = sra_meta.get("bioproject_accession")
        if bioproject_id:
            bp_meta = await _fetch_bioproject(bioproject_id)
            bp_meta["resolved_from"] = accession
            return bp_meta
        else:
            # Fallback: try elink
            bioproject_id = await _resolve_bioproject_via_elink("sra", accession)
            if bioproject_id:
                bp_meta = await _fetch_bioproject(bioproject_id)
                bp_meta["resolved_from"] = accession
                return bp_meta
            return {"error": f"Could not resolve {accession} to a BioProject", "accession": accession}

    # BioSample accessions (NCBI, EBI, DDBJ)
    if accession.startswith(("SAMN", "SAME", "SAMD", "SAMEA")):
        bioproject_id = await _resolve_bioproject_from_biosample(accession)
        if bioproject_id:
            bp_meta = await _fetch_bioproject(bioproject_id)
            bp_meta["resolved_from"] = accession
            return bp_meta
        return {"error": f"Could not resolve {accession} to a BioProject", "accession": accession}

    return {"error": f"Unrecognized accession format: {accession}", "accession": accession}


async def _resolve_bioproject_from_biosample(accession: str) -> Optional[str]:
    """Resolve a BioSample accession to its parent BioProject.

    Tries two strategies:
      1. Parse BioProject link directly from BioSample XML
      2. Find linked SRA accession and resolve through SRA path
    """
    try:
        search_handle = Entrez.esearch(db="biosample", term=accession)
        search_results = Entrez.read(search_handle)
        search_handle.close()

        if not search_results["IdList"]:
            return None

        bs_id = search_results["IdList"][0]
        fetch_handle = Entrez.efetch(db="biosample", id=bs_id, rettype="full", retmode="xml")
        xml_data = fetch_handle.read()
        fetch_handle.close()

        if isinstance(xml_data, str):
            xml_data = xml_data.encode()

        root = ElementTree.fromstring(xml_data)

        # Strategy 1: direct BioProject link
        for link in root.iter("Link"):
            if link.get("target") == "bioproject":
                label = link.get("label", "")
                if label.startswith(("PRJNA", "PRJEB", "PRJDB")):
                    return label

        # Strategy 2: find linked SRA accession (EBI samples store ERS in Ids)
        for id_el in root.iter("Id"):
            if id_el.get("db") == "SRA" and id_el.text:
                sra_acc = id_el.text
                sra_meta = await _fetch_sra(sra_acc)
                bp = sra_meta.get("bioproject_accession")
                if bp:
                    return bp

        return None
    except Exception:
        return None


async def fetch_sra_metadata(accession: str) -> dict:
    """
    Fetch full metadata for an SRA/BioSample/BioProject accession from NCBI.

    Supports:
      SRR/ERR/DRR (run), SRX (experiment), SRP (study) — SRA database
      PRJNA (BioProject) — BioProject database
      SAMN/SAME/SAMD (BioSample) — BioSample database
    Returns parsed metadata dict.
    """
    accession = accession.strip().upper()

    # Determine which database to query
    if accession.startswith(("PRJNA", "PRJEB", "PRJDB")):
        return await _fetch_bioproject(accession)
    elif accession.startswith(("SAMN", "SAME", "SAMD", "SAMEA")):
        return await _fetch_biosample(accession)
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
        # Ask by ACCESSION FIELD, not free text. A bare accession is a full-text
        # query, so it also matches projects that merely cite it in their
        # description — searching PRJNA895866 returns two hits, and the unrelated
        # project sorts first. Taking IdList[0] then labels the submission with a
        # stranger's title and abstract, which is what the manuscript generator
        # and autoresearch are handed as the study context.
        search_handle = Entrez.esearch(db="bioproject", term=f"{accession}[Project Accession]")
        search_results = Entrez.read(search_handle)
        search_handle.close()

        if not search_results["IdList"]:
            return {"error": f"BioProject {accession} not found", "accession": accession}

        bp_id = search_results["IdList"][0]

        summary_handle = Entrez.esummary(db="bioproject", id=bp_id)
        summary = Entrez.read(summary_handle)
        summary_handle.close()

        project_data = summary.get("DocumentSummarySet", {}).get("DocumentSummary", [{}])[0]

        # Belt and braces: never describe a project with another one's text.
        returned_acc = str(project_data.get("Project_Acc", "") or "")
        if returned_acc and returned_acc.upper() != accession.upper():
            project_data = {}

        # Find all linked SRA runs via esearch
        # First get the count, then fetch all IDs
        sra_count_search = Entrez.esearch(db="sra", term=accession, retmax=0)
        sra_count_results = Entrez.read(sra_count_search)
        sra_count_search.close()
        total_runs = int(sra_count_results.get("Count", 0))

        sra_ids = []
        # Fetch IDs in batches of 10000 (NCBI esearch max)
        for start in range(0, total_runs, 10000):
            sra_search = Entrez.esearch(db="sra", term=accession, retmax=10000, retstart=start)
            sra_results = Entrez.read(sra_search)
            sra_search.close()
            sra_ids.extend(sra_results.get("IdList", []))

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

        # Summarize ALL runs via esummary (lightweight, ~200 bytes per run)
        if sra_ids:
            project_summary = _summarize_sra_esummary(sra_ids)
            metadata.update(project_summary)

        return metadata

    except Exception as e:
        return {"error": str(e), "accession": accession}


async def _fetch_biosample(accession: str) -> dict:
    """Fetch metadata from BioSample database."""
    try:
        # Search BioSample
        search_handle = Entrez.esearch(db="biosample", term=accession)
        search_results = Entrez.read(search_handle)
        search_handle.close()

        if not search_results["IdList"]:
            return {"error": f"BioSample {accession} not found", "accession": accession}

        bs_id = search_results["IdList"][0]

        # Fetch full BioSample record
        fetch_handle = Entrez.efetch(db="biosample", id=bs_id, rettype="full", retmode="xml")
        xml_data = fetch_handle.read()
        fetch_handle.close()

        metadata = _parse_biosample_xml(xml_data, accession)

        # Check if there are linked SRA runs
        sra_search = Entrez.esearch(db="sra", term=accession)
        sra_results = Entrez.read(sra_search)
        sra_search.close()

        sra_ids = sra_results.get("IdList", [])
        metadata["num_sra_runs"] = int(sra_results.get("Count", 0))

        # If there are SRA runs, fetch the first for sequencing details
        if sra_ids:
            run_handle = Entrez.efetch(db="sra", id=sra_ids[0], rettype="full", retmode="xml")
            run_xml = run_handle.read()
            run_handle.close()
            run_metadata = _parse_sra_xml(run_xml, accession)

            for field in ["platform", "instrument_model", "library_strategy",
                          "library_source", "library_layout", "run_accession",
                          "total_spots", "total_bases", "size_mb",
                          "study_title", "study_accession"]:
                if field in run_metadata:
                    metadata[field] = run_metadata[field]

        return metadata

    except Exception as e:
        return {"error": str(e), "accession": accession}


def _parse_biosample_xml(xml_data: str | bytes, accession: str) -> dict:
    """Parse BioSample XML into a clean metadata dict."""
    if isinstance(xml_data, str):
        xml_data = xml_data.encode()

    root = ElementTree.fromstring(xml_data)

    metadata = {
        "accession": accession,
        "type": "BioSample",
    }

    for biosample in root.iter("BioSample"):
        metadata["biosample_id"] = biosample.get("accession", accession)

        # Title / description
        title_el = biosample.find(".//Title")
        if title_el is not None:
            metadata["sample_title"] = title_el.text

        # Organism
        org_el = biosample.find(".//Organism")
        if org_el is not None:
            metadata["organism"] = org_el.get("taxonomy_name", "")
            metadata["taxon_id"] = org_el.get("taxonomy_id", "")

        # Owner/submitter
        owner_el = biosample.find(".//Owner/Name")
        if owner_el is not None:
            metadata["organization"] = owner_el.text

        # Model (e.g., MIGS/MIMS/MIMARKS)
        model_el = biosample.find(".//Model")
        if model_el is not None:
            metadata["biosample_model"] = model_el.text

        # Package
        pkg_el = biosample.find(".//Package")
        if pkg_el is not None:
            metadata["biosample_package"] = pkg_el.get("display_name", pkg_el.text or "")

        # Sample attributes
        attrs = {}
        for attr in biosample.iter("Attribute"):
            name = attr.get("harmonized_name") or attr.get("attribute_name", "")
            if name and attr.text:
                attrs[name] = attr.text
        metadata["sample_attributes"] = attrs

        # Only parse first BioSample
        break

    return metadata


def _summarize_sra_esummary(sra_ids: list[str]) -> dict:
    """Summarize all SRA runs using esummary (lightweight per-run metadata).

    Returns a breakdown table: each unique (platform, instrument, strategy,
    source, layout) combination gets run and sample counts plus data totals.
    Also returns aggregate organism list.
    """
    from collections import defaultdict

    # Per-combination tracking
    # Key: (platform, instrument, strategy, source, layout)
    combo_runs = defaultdict(int)
    combo_samples = defaultdict(set)
    combo_bases = defaultdict(int)
    combo_spots = defaultdict(int)
    combo_accessions = defaultdict(list)  # actual SRR/ERR run accessions

    organisms = set()
    all_samples = set()
    total_bases = 0
    total_spots = 0

    # Process in batches of 200 (NCBI esummary limit)
    for i in range(0, len(sra_ids), 200):
        batch = sra_ids[i:i + 200]
        try:
            summary_handle = Entrez.esummary(db="sra", id=",".join(batch))
            summaries = Entrez.read(summary_handle)
            summary_handle.close()
        except Exception:
            continue

        for record in summaries:
            exp_xml = record.get("ExpXml", "")
            runs_xml = record.get("Runs", "")

            try:
                frag = ElementTree.fromstring(f"<root>{exp_xml}{runs_xml}</root>")
            except ElementTree.ParseError:
                continue

            # Extract fields for this run
            platform = ""
            instrument = ""
            plat_el = frag.find(".//Platform")
            if plat_el is not None:
                platform = plat_el.text or ""
                instrument = plat_el.get("instrument_model", "")

            strategy = ""
            strat_el = frag.find(".//LIBRARY_STRATEGY")
            if strat_el is not None and strat_el.text:
                strategy = strat_el.text

            source = ""
            src_el = frag.find(".//LIBRARY_SOURCE")
            if src_el is not None and src_el.text:
                source = src_el.text

            layout = ""
            layout_el = frag.find(".//LIBRARY_LAYOUT")
            if layout_el is not None and len(layout_el) > 0:
                layout = list(layout_el)[0].tag

            # Organism
            org_el = frag.find(".//Organism")
            if org_el is not None:
                name = org_el.get("ScientificName", "")
                if name:
                    organisms.add(name)

            # Sample accession
            sample_acc = ""
            bs_el = frag.find(".//Biosample")
            if bs_el is not None and bs_el.text:
                sample_acc = bs_el.text
            else:
                sample_el = frag.find(".//Sample")
                if sample_el is not None:
                    sample_acc = sample_el.get("acc", "")

            if sample_acc:
                all_samples.add(sample_acc)

            # Run data
            run_bases = 0
            run_spots = 0
            run_accs = []
            for run_el in frag.iter("Run"):
                try:
                    run_spots += int(run_el.get("total_spots", "0"))
                    run_bases += int(run_el.get("total_bases", "0"))
                except ValueError:
                    pass
                acc = run_el.get("acc", "")
                if acc:
                    run_accs.append(acc)

            total_bases += run_bases
            total_spots += run_spots

            # Accumulate per combination
            key = (platform, instrument, strategy, source, layout)
            combo_runs[key] += 1
            if sample_acc:
                combo_samples[key].add(sample_acc)
            combo_bases[key] += run_bases
            combo_spots[key] += run_spots
            combo_accessions[key].extend(run_accs)

    # Build breakdown table (list of dicts, sorted by run count desc)
    breakdown = []
    for key in sorted(combo_runs, key=lambda k: combo_runs[k], reverse=True):
        platform, instrument, strategy, source, layout = key
        breakdown.append({
            "platform": platform,
            "instrument": instrument,
            "strategy": strategy,
            "source": source,
            "layout": layout,
            "runs": combo_runs[key],
            "samples": len(combo_samples[key]),
            "bases": combo_bases[key],
            "spots": combo_spots[key],
            "run_accessions": combo_accessions[key],
        })

    summary = {
        "breakdown": breakdown,
        "num_samples": len(all_samples),
    }

    if organisms:
        summary["organism"] = ", ".join(sorted(organisms)) if len(organisms) <= 3 else f"{sorted(organisms)[0]} + {len(organisms)-1} others"

    # Convenience: flatten unique values for pipeline auto-selection
    summary["library_strategy"] = ", ".join(sorted({b["strategy"] for b in breakdown if b["strategy"]}))
    summary["platform"] = ", ".join(sorted({b["platform"] for b in breakdown if b["platform"]}))
    summary["library_source"] = ", ".join(sorted({b["source"] for b in breakdown if b["source"]}))
    summary["library_layout"] = ", ".join(sorted({b["layout"] for b in breakdown if b["layout"]}))
    summary["instrument_model"] = ", ".join(sorted({b["instrument"] for b in breakdown if b["instrument"]}))

    if total_bases > 0:
        summary["total_bases"] = total_bases
        summary["total_spots"] = total_spots

    return summary


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

            # Extract BioProject accession from EXTERNAL_ID
            for ext_id in study.iter("EXTERNAL_ID"):
                if ext_id.get("namespace") == "BioProject" and ext_id.text:
                    metadata["bioproject_accession"] = ext_id.text
                    break

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


async def fetch_sample_metadata(bioproject: str) -> list[dict]:
    """Fetch per-sample metadata for a BioProject from EBI/ENA.

    Returns a list of dicts, one per run, with MIxS/MIMARKS fields:
    collection_date, country, lat, lon, environment_biome/feature/material,
    plus sequencing metadata (platform, strategy, bases, etc.).

    ENA mirrors NCBI data so PRJNA accessions work directly.
    Falls back to NCBI BioSample fetch if ENA returns nothing.
    """
    samples = await _fetch_ena_metadata(bioproject)
    if not samples:
        samples = await _fetch_biosample_metadata_ncbi(bioproject)
    return samples


async def _fetch_ena_metadata(bioproject: str) -> list[dict]:
    """Fetch per-run metadata from EBI/ENA portal API as TSV."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(ENA_API, params={
                "accession": bioproject,
                "result": "read_run",
                "fields": ENA_FIELDS,
                "format": "tsv",
                "limit": "0",  # no limit
            })
            if resp.status_code != 200:
                logger.warning(f"ENA API returned {resp.status_code} for {bioproject}")
                return []

            text = resp.text.strip()
            if not text or "\t" not in text:
                return []

            reader = csv.DictReader(io.StringIO(text), delimiter="\t")
            samples = []
            # Columns to exclude — download URLs, hashes, and file paths
            # that bloat storage without adding scientific value
            _skip = {
                "fastq_ftp", "fastq_aspera", "fastq_galaxy", "fastq_md5", "fastq_bytes",
                "submitted_ftp", "submitted_aspera", "submitted_galaxy", "submitted_md5", "submitted_bytes",
                "sra_ftp", "sra_aspera", "sra_galaxy", "sra_md5", "sra_bytes",
                "bam_ftp", "bam_aspera", "bam_galaxy", "bam_md5", "bam_bytes", "bam_file_role",
                "cram_index_aspera", "cram_index_ftp", "cram_index_galaxy",
            }

            for row in reader:
                # Strip empty values and skip download/hash columns
                clean = {k: v for k, v in row.items()
                         if v and v.strip() and k not in _skip}
                if clean:
                    samples.append(clean)

            logger.info(f"ENA returned {len(samples)} runs for {bioproject}")
            return samples

    except Exception as e:
        logger.warning(f"ENA fetch failed for {bioproject}: {e}")
        return []


async def _fetch_biosample_metadata_ncbi(bioproject: str) -> list[dict]:
    """Fallback: fetch BioSample records from NCBI for a BioProject.

    Slower than ENA but works for accessions not yet mirrored.
    """
    try:
        # Find linked BioSample IDs
        link_handle = Entrez.elink(
            dbfrom="bioproject", db="biosample",
            id=bioproject, linkname="bioproject_biosample",
        )
        links = Entrez.read(link_handle)
        link_handle.close()

        bs_ids = []
        for linkset in links:
            for link_db in linkset.get("LinkSetDb", []):
                bs_ids.extend(link.get("Id", "") for link in link_db.get("Link", []))

        if not bs_ids:
            logger.info(f"No BioSamples linked to {bioproject}")
            return []

        # Fetch BioSample records in batches
        samples = []
        for i in range(0, len(bs_ids), 100):
            batch = bs_ids[i:i + 100]
            fetch_handle = Entrez.efetch(
                db="biosample", id=",".join(batch),
                rettype="full", retmode="xml",
            )
            xml_data = fetch_handle.read()
            fetch_handle.close()

            if isinstance(xml_data, str):
                xml_data = xml_data.encode()

            root = ElementTree.fromstring(xml_data)
            for biosample in root.iter("BioSample"):
                sample = {
                    "sample_accession": biosample.get("accession", ""),
                }

                title_el = biosample.find(".//Title")
                if title_el is not None and title_el.text:
                    sample["sample_title"] = title_el.text

                org_el = biosample.find(".//Organism")
                if org_el is not None:
                    sample["scientific_name"] = org_el.get("taxonomy_name", "")
                    sample["tax_id"] = org_el.get("taxonomy_id", "")

                model_el = biosample.find(".//Model")
                if model_el is not None and model_el.text:
                    sample["biosample_model"] = model_el.text

                # All sample attributes (MIxS/MIMARKS fields)
                for attr in biosample.iter("Attribute"):
                    name = attr.get("harmonized_name") or attr.get("attribute_name", "")
                    if name and attr.text:
                        sample[name] = attr.text

                samples.append(sample)

        logger.info(f"NCBI returned {len(samples)} BioSamples for {bioproject}")
        return samples

    except Exception as e:
        logger.warning(f"NCBI BioSample fetch failed for {bioproject}: {e}")
        return []


def metadata_summary(metadata: dict) -> str:
    """Generate a human-readable summary of the metadata for display."""
    if "error" in metadata:
        return f"Error fetching metadata: {metadata['error']}"

    lines = []
    lines.append(f"Accession: {metadata.get('accession', 'Unknown')}")
    meta_type = metadata.get("type", "")
    if meta_type:
        lines.append(f"Type: {meta_type}")

    if metadata.get("title"):
        lines.append(f"Title: {metadata['title']}")
    if metadata.get("study_title"):
        lines.append(f"Study: {metadata['study_title']}")
    if metadata.get("sample_title"):
        lines.append(f"Sample: {metadata['sample_title']}")
    if metadata.get("description"):
        lines.append(f"Description: {metadata['description']}")
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
