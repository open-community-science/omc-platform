"""ENA submission helpers — field mapping, XML builders, validation.

Pure logic module. The XML builders produce ENA-compliant XML for the
programmatic submission endpoint. The portal ENA router submits these
via HTTP POST with Content-Type: application/xml.

Dynamic checklist fetching: `fetch_checklist_fields()` hits the ENA
Browser API to get live field definitions (mandatory, regex, enums)
so we never fall out of sync with ENA's requirements.
"""

import re
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any

# ── NCBI ↔ ENA field mapping ─────────────────────────────────────────────────

NCBI_TO_ENA_FIELDS: dict[str, str] = {
    "sample_name": "sample_alias",
    "organism": "scientific_name",
    "collection_date": "collection date",
    "geo_loc_name": "geographic location (country and/or sea)",
    "lat_lon": "geographic location (latitude)",  # split into lat + lon
    "isolation_source": "isolation_source",
    "env_broad_scale": "broad-scale environmental context",
    "env_local_scale": "local-scale environmental context",
    "env_medium": "environmental medium",
    "host": "host scientific name",
    "depth": "depth",
    "elev": "elevation",
    "temp": "temperature",
    "ph": "pH",
    "description": "sample_description",
    "title": "sample_title",
}

ENA_TO_NCBI_FIELDS: dict[str, str] = {v: k for k, v in NCBI_TO_ENA_FIELDS.items()}
# lat/lon needs special handling — also map the longitude key back
ENA_TO_NCBI_FIELDS["geographic location (longitude)"] = "lat_lon"


def _parse_lat_lon(lat_lon: str) -> tuple[str, str]:
    """Parse NCBI lat_lon '47.6 N 122.3 W' into (lat, lon) strings."""
    m = re.match(
        r"^\s*([0-9.+-]+)\s*([NSns])\s+([0-9.+-]+)\s*([EWew])\s*$",
        lat_lon.strip(),
    )
    if not m:
        return lat_lon, ""
    lat_val, lat_dir, lon_val, lon_dir = m.groups()
    lat = f"{lat_val} {lat_dir.upper()}"
    lon = f"{lon_val} {lon_dir.upper()}"
    return lat, lon


def convert_ncbi_to_ena(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a single NCBI metadata row to ENA field names."""
    out: dict[str, Any] = {}
    for ncbi_key, value in row.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        ena_key = NCBI_TO_ENA_FIELDS.get(ncbi_key)
        if ncbi_key == "lat_lon" and isinstance(value, str):
            lat, lon = _parse_lat_lon(value)
            out["geographic location (latitude)"] = lat
            out["geographic location (longitude)"] = lon
        elif ena_key:
            out[ena_key] = value
        else:
            out[ncbi_key] = value
    return out


def convert_ena_to_ncbi(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a single ENA metadata row to NCBI field names."""
    out: dict[str, Any] = {}
    lat = lon = ""
    for ena_key, value in row.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if ena_key == "geographic location (latitude)":
            lat = str(value)
            continue
        if ena_key == "geographic location (longitude)":
            lon = str(value)
            continue
        ncbi_key = ENA_TO_NCBI_FIELDS.get(ena_key, ena_key)
        out[ncbi_key] = value
    if lat or lon:
        out["lat_lon"] = f"{lat} {lon}".strip()
    return out


# ── ENA checklists (hardcoded fallback + keyword mapping) ────────────────────

ENA_CHECKLISTS: dict[str, dict[str, Any]] = {
    "ERC000011": {
        "name": "ENA default sample checklist",
        "required": ["scientific_name", "collection date"],
        "keywords": ["default", "general"],
    },
    "ERC000024": {
        "name": "Water",
        "required": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "depth",
        ],
        "keywords": ["water", "aquatic", "marine", "freshwater", "ocean", "lake", "river"],
    },
    "ERC000014": {
        "name": "GSC MIxS soil",
        "required": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "depth", "elevation",
        ],
        "keywords": ["soil", "terrestrial", "earth"],
    },
    "ERC000028": {
        "name": "GSC MIxS sediment",
        "required": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "depth",
        ],
        "keywords": ["sediment"],
    },
    "ERC000013": {
        "name": "GSC MIxS host associated",
        "required": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "host scientific name",
        ],
        "keywords": ["host-associated", "host", "symbiont", "endophyte", "microbiome"],
    },
    "ERC000052": {
        "name": "GSC MIxS human associated",
        "required": [
            "scientific_name", "collection date",
            "geographic location (country and/or sea)",
            "geographic location (latitude)", "geographic location (longitude)",
            "broad-scale environmental context",
            "local-scale environmental context", "environmental medium",
            "host scientific name",
        ],
        "keywords": ["human", "clinical", "patient", "gut", "oral", "skin"],
    },
}

# Common optional fields across most checklists
COMMON_OPTIONAL = [
    "sample_alias", "sample_title", "sample_description", "tax_id",
    "isolation_source", "temperature", "pH", "salinity",
    "host scientific name", "elevation",
]


def resolve_checklist(environment_type: str) -> str:
    """Map a free-text environment description to an ENA checklist code."""
    env_lower = environment_type.lower().strip()
    for code, info in ENA_CHECKLISTS.items():
        for kw in info["keywords"]:
            if kw in env_lower:
                return code
    return "ERC000011"  # default


# ── Dynamic checklist fetching from ENA ──────────────────────────────────────

ENA_BROWSER_API = "https://www.ebi.ac.uk/ena/browser/api/xml"

# Cache parsed checklists to avoid repeated HTTP calls
_checklist_cache: dict[str, dict[str, Any]] = {}


def parse_checklist_xml(xml_text: str) -> dict[str, Any]:
    """Parse an ENA checklist XML into a structured dict.

    Returns:
        {
            "checklist_id": "ERC000024",
            "name": "GSC MIxS water",
            "description": "...",
            "fields": {
                "field_name": {
                    "description": "...",
                    "mandatory": True/False,
                    "type": "text" | "choice" | "regex",
                    "regex": "..." or None,
                    "enum_values": [...] or None,
                    "units": [...] or None,
                },
                ...
            }
        }
    """
    root = ET.fromstring(xml_text)

    # Find the CHECKLIST element (may be root or nested)
    checklist = root.find(".//CHECKLIST")
    if checklist is None:
        checklist = root  # some responses wrap differently

    checklist_id = checklist.get("accession", "")
    descriptor = checklist.find("DESCRIPTOR")
    name = ""
    description = ""
    if descriptor is not None:
        name_el = descriptor.find("NAME")
        if name_el is not None and name_el.text:
            name = name_el.text.strip()
        desc_el = descriptor.find("DESCRIPTION")
        if desc_el is not None and desc_el.text:
            description = desc_el.text.strip()

    fields: dict[str, dict[str, Any]] = {}

    for field_el in checklist.iter("FIELD"):
        label_el = field_el.find("LABEL")
        if label_el is None:
            label_el = field_el.find("NAME")
        if label_el is None or not label_el.text:
            continue
        field_name = label_el.text.strip()

        desc_el = field_el.find("DESCRIPTION")
        field_desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

        mandatory_el = field_el.find("MANDATORY")
        mandatory = (
            mandatory_el is not None
            and mandatory_el.text
            and mandatory_el.text.strip().lower() == "mandatory"
        )

        # Determine field type
        field_type = "text"
        regex_val = None
        enum_values = None

        regex_el = field_el.find(".//REGEX_VALUE")
        if regex_el is not None and regex_el.text:
            field_type = "regex"
            regex_val = regex_el.text.strip()

        choice_els = field_el.findall(".//TEXT_CHOICE_FIELD/TEXT_VALUE/VALUE")
        if choice_els:
            field_type = "choice"
            enum_values = [e.text.strip() for e in choice_els if e.text]

        # Units
        units = None
        unit_els = field_el.findall(".//UNIT")
        if unit_els:
            units = [u.text.strip() for u in unit_els if u.text]

        fields[field_name] = {
            "description": field_desc,
            "mandatory": mandatory,
            "type": field_type,
            "regex": regex_val,
            "enum_values": enum_values,
            "units": units,
        }

    return {
        "checklist_id": checklist_id,
        "name": name,
        "description": description,
        "fields": fields,
    }


async def fetch_checklist_fields(checklist_id: str) -> dict[str, Any] | None:
    """Fetch and parse a checklist from ENA Browser API.

    Returns parsed checklist dict or None on failure. Results are cached.
    """
    if checklist_id in _checklist_cache:
        return _checklist_cache[checklist_id]

    import httpx
    url = f"{ENA_BROWSER_API}/{checklist_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        parsed = parse_checklist_xml(resp.text)
        _checklist_cache[checklist_id] = parsed
        return parsed
    except Exception:
        return None


def get_checklist_required_fields(parsed: dict[str, Any]) -> list[str]:
    """Extract mandatory field names from a parsed checklist."""
    return [
        name for name, info in parsed.get("fields", {}).items()
        if info.get("mandatory")
    ]


def validate_against_checklist(
    rows: list[dict[str, Any]],
    parsed_checklist: dict[str, Any],
) -> dict[str, Any]:
    """Validate metadata rows against a live-fetched checklist.

    Uses the full field definitions (mandatory, regex, enum) from ENA
    rather than our hardcoded subset.
    """
    fields = parsed_checklist.get("fields", {})
    errors: list[str] = []
    warnings: list[str] = []

    if not rows:
        return {"valid": False, "errors": ["No sample rows provided"], "warnings": []}

    for i, row in enumerate(rows, 1):
        alias = row.get("sample_alias", row.get("alias", f"row {i}"))

        for field_name, field_def in fields.items():
            val = row.get(field_name)
            val_str = str(val).strip() if val is not None else ""

            if field_def["mandatory"] and not val_str:
                errors.append(f"Sample '{alias}': missing required field '{field_name}'")
                continue

            if not val_str:
                continue

            # Regex validation
            if field_def["type"] == "regex" and field_def.get("regex"):
                if not re.match(field_def["regex"], val_str):
                    warnings.append(
                        f"Sample '{alias}': field '{field_name}' value '{val_str}' "
                        f"doesn't match pattern {field_def['regex']}"
                    )

            # Enum validation
            if field_def["type"] == "choice" and field_def.get("enum_values"):
                if val_str not in field_def["enum_values"]:
                    warnings.append(
                        f"Sample '{alias}': field '{field_name}' value '{val_str}' "
                        f"not in allowed values: {field_def['enum_values'][:5]}..."
                    )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── XML builders ─────────────────────────────────────────────────────────────
# ENA's programmatic submission endpoint expects XML, not JSON.
# These functions build ElementTree objects, then serialize to bytes.

def _xml_to_bytes(root: ET.Element) -> bytes:
    """Serialize an ElementTree element to UTF-8 XML bytes."""
    tree = ET.ElementTree(root)
    buf = BytesIO()
    tree.write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


def build_submission_xml(action: str = "ADD", hold_date: str | None = None) -> bytes:
    """Build the SUBMISSION XML wrapper that accompanies every ENA submission.

    action: ADD, MODIFY, VALIDATE
    """
    root = ET.Element("SUBMISSION")
    actions_el = ET.SubElement(root, "ACTIONS")
    action_el = ET.SubElement(actions_el, "ACTION")
    ET.SubElement(action_el, action.upper())
    if hold_date:
        hold_action = ET.SubElement(actions_el, "ACTION")
        ET.SubElement(hold_action, "HOLD", HoldUntilDate=hold_date)
    return _xml_to_bytes(root)


def build_project_xml(
    alias: str,
    title: str,
    description: str,
) -> bytes:
    """Build a PROJECT_SET XML for study registration."""
    root = ET.Element("PROJECT_SET")
    project = ET.SubElement(root, "PROJECT", alias=alias)
    ET.SubElement(project, "TITLE").text = title
    ET.SubElement(project, "DESCRIPTION").text = description
    sub_proj = ET.SubElement(project, "SUBMISSION_PROJECT")
    ET.SubElement(sub_proj, "SEQUENCING_PROJECT")
    return _xml_to_bytes(root)


def build_sample_xml(
    samples: list[dict[str, Any]],
    checklist: str,
) -> bytes:
    """Build a SAMPLE_SET XML for sample registration.

    Each sample dict should have ENA field names. Special keys:
    - sample_alias / alias → SAMPLE alias attribute
    - tax_id → TAXON_ID
    - scientific_name → SCIENTIFIC_NAME
    - Everything else → SAMPLE_ATTRIBUTE tags
    """
    root = ET.Element("SAMPLE_SET")
    skip_keys = {"sample_alias", "alias", "tax_id", "scientific_name", "checklist"}

    for s in samples:
        alias = s.get("sample_alias", s.get("alias", ""))
        sample = ET.SubElement(root, "SAMPLE", alias=alias)

        ET.SubElement(sample, "TITLE").text = s.get("sample_title", alias)

        name_el = ET.SubElement(sample, "SAMPLE_NAME")
        if "tax_id" in s and s["tax_id"]:
            ET.SubElement(name_el, "TAXON_ID").text = str(s["tax_id"])
        if "scientific_name" in s and s["scientific_name"]:
            ET.SubElement(name_el, "SCIENTIFIC_NAME").text = str(s["scientific_name"])

        attrs = ET.SubElement(sample, "SAMPLE_ATTRIBUTES")

        # Checklist attribute (tells ENA which checklist to validate against)
        attr = ET.SubElement(attrs, "SAMPLE_ATTRIBUTE")
        ET.SubElement(attr, "TAG").text = "ENA-CHECKLIST"
        ET.SubElement(attr, "VALUE").text = checklist

        for k, v in s.items():
            if k in skip_keys or v is None:
                continue
            v_str = str(v).strip()
            if not v_str:
                continue
            attr = ET.SubElement(attrs, "SAMPLE_ATTRIBUTE")
            ET.SubElement(attr, "TAG").text = k
            ET.SubElement(attr, "VALUE").text = v_str

    return _xml_to_bytes(root)


def build_experiment_xml(experiments: list[dict[str, Any]]) -> bytes:
    """Build an EXPERIMENT_SET XML.

    Each experiment dict should have:
        - alias, study_accession, sample_accession
        - library_strategy, library_source, library_selection
        - platform, instrument
    """
    root = ET.Element("EXPERIMENT_SET")
    for exp in experiments:
        alias = exp.get("alias", exp.get("sample_accession", ""))
        experiment = ET.SubElement(root, "EXPERIMENT", alias=alias)

        ET.SubElement(experiment, "TITLE").text = exp.get("title", alias)

        study_ref = ET.SubElement(experiment, "STUDY_REF")
        study_ref.set("accession", exp["study_accession"])

        design = ET.SubElement(experiment, "DESIGN")
        ET.SubElement(design, "DESIGN_DESCRIPTION")
        sample_desc = ET.SubElement(design, "SAMPLE_DESCRIPTOR")
        sample_desc.set("accession", exp["sample_accession"])

        lib_desc = ET.SubElement(design, "LIBRARY_DESCRIPTOR")
        ET.SubElement(lib_desc, "LIBRARY_STRATEGY").text = exp.get("library_strategy", "WGS")
        ET.SubElement(lib_desc, "LIBRARY_SOURCE").text = exp.get("library_source", "METAGENOMIC")
        ET.SubElement(lib_desc, "LIBRARY_SELECTION").text = exp.get("library_selection", "RANDOM")
        lib_layout = ET.SubElement(lib_desc, "LIBRARY_LAYOUT")
        ET.SubElement(lib_layout, "SINGLE")  # default; could check for paired

        platform_el = ET.SubElement(experiment, "PLATFORM")
        plat_name = exp.get("platform", "OXFORD_NANOPORE")
        plat = ET.SubElement(platform_el, plat_name)
        ET.SubElement(plat, "INSTRUMENT_MODEL").text = exp.get("instrument", "MinION")

    return _xml_to_bytes(root)


def build_run_xml(runs: list[dict[str, Any]]) -> bytes:
    """Build a RUN_SET XML.

    Each run dict should have:
        - alias, experiment_alias (or experiment_accession)
        - files: list of {filename, filetype, checksum_method, checksum}
    """
    root = ET.Element("RUN_SET")
    for run in runs:
        alias = run.get("alias", "")
        run_el = ET.SubElement(root, "RUN", alias=alias)

        exp_ref = ET.SubElement(run_el, "EXPERIMENT_REF")
        if "experiment_accession" in run:
            exp_ref.set("accession", run["experiment_accession"])
        else:
            exp_ref.set("refname", run.get("experiment_alias", alias))

        data_block = ET.SubElement(run_el, "DATA_BLOCK")
        files_el = ET.SubElement(data_block, "FILES")
        for f in run.get("files", []):
            file_el = ET.SubElement(files_el, "FILE")
            file_el.set("filename", f["filename"])
            file_el.set("filetype", f.get("filetype", "fastq"))
            file_el.set("checksum_method", f.get("checksum_method", "MD5"))
            file_el.set("checksum", f.get("checksum", ""))

    return _xml_to_bytes(root)


# ── Receipt parsing ──────────────────────────────────────────────────────────

def parse_receipt_xml(xml_text: str) -> dict[str, Any]:
    """Parse an ENA submission receipt XML into a structured result.

    Returns:
        {
            "success": True/False,
            "accessions": {"alias": "accession", ...},
            "messages": {"info": [...], "error": [...]},
            "receipt_xml": "..." (raw XML for debugging)
        }
    """
    result: dict[str, Any] = {
        "success": False,
        "accessions": {},
        "messages": {"info": [], "error": []},
        "receipt_xml": xml_text,
    }

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        result["messages"]["error"].append(f"Failed to parse receipt XML")
        return result

    # Check success attribute on RECEIPT element
    receipt = root if root.tag == "RECEIPT" else root.find(".//RECEIPT")
    if receipt is not None:
        result["success"] = receipt.get("success", "false").lower() == "true"

        # Extract accessions from various object types
        for obj_tag in ("STUDY", "SAMPLE", "EXPERIMENT", "RUN", "PROJECT"):
            for obj in receipt.findall(f".//{obj_tag}"):
                alias = obj.get("alias", "")
                accession = obj.get("accession", "")
                if alias and accession:
                    result["accessions"][alias] = accession
                # Also check for EXT_ID (external IDs like BioSample)
                for ext in obj.findall("EXT_ID"):
                    ext_type = ext.get("type", "")
                    ext_acc = ext.get("accession", "")
                    if ext_type and ext_acc:
                        result["accessions"][f"{alias}:{ext_type}"] = ext_acc

    # Extract info/error messages
    for msg in root.iter("INFO"):
        if msg.text:
            result["messages"]["info"].append(msg.text.strip())
    for msg in root.iter("ERROR"):
        if msg.text:
            result["messages"]["error"].append(msg.text.strip())

    return result


# ── Legacy dict payload builders (kept for backward compat with tests) ───────

def build_project_payload(
    alias: str,
    title: str,
    description: str,
    hold_date: str | None = None,
) -> dict:
    """Build a project registration payload as a dict."""
    payload: dict[str, Any] = {
        "alias": alias,
        "title": title,
        "description": description,
        "studyType": "Metagenomics",
    }
    if hold_date:
        payload["holdDate"] = hold_date
    return payload


def build_samples_payload(
    samples: list[dict[str, Any]],
    checklist: str,
    action: str = "ADD",
) -> dict:
    """Build a sample submission payload as a dict."""
    sample_objects = []
    for s in samples:
        obj: dict[str, Any] = {
            "alias": s.get("sample_alias", s.get("alias", "")),
            "checklist": checklist,
            "attributes": {},
        }
        if "tax_id" in s:
            obj["taxId"] = int(s["tax_id"])
        if "scientific_name" in s:
            obj["scientificName"] = s["scientific_name"]
        skip = {"sample_alias", "alias", "tax_id", "scientific_name", "checklist"}
        for k, v in s.items():
            if k not in skip and v is not None:
                obj["attributes"][k] = [{"value": str(v)}]
        sample_objects.append(obj)
    return {"action": action, "samples": sample_objects}


def build_experiment_run_payload(experiments: list[dict[str, Any]]) -> dict:
    """Build experiment + run submission payload as a dict."""
    experiments_out = []
    for exp in experiments:
        exp_obj: dict[str, Any] = {
            "alias": exp.get("alias", exp.get("sample_accession", "")),
            "studyRef": exp["study_accession"],
            "sampleRef": exp["sample_accession"],
            "libraryDescriptor": {
                "libraryStrategy": exp.get("library_strategy", "WGS"),
                "librarySource": exp.get("library_source", "METAGENOMIC"),
                "librarySelection": exp.get("library_selection", "RANDOM"),
            },
            "platform": exp.get("platform", "OXFORD_NANOPORE"),
            "instrumentModel": exp.get("instrument", "MinION"),
        }
        runs = []
        for f in exp.get("files", []):
            runs.append({
                "filename": f["filename"],
                "filetype": f.get("filetype", "fastq"),
                "checksumMethod": f.get("checksum_method", "MD5"),
                "checksum": f.get("checksum", ""),
            })
        exp_obj["runs"] = runs
        experiments_out.append(exp_obj)
    return {"experiments": experiments_out}


def build_modify_payload(
    object_type: str,
    accession: str,
    updates: dict[str, Any],
) -> dict:
    """Build a MODIFY payload as a dict."""
    return {
        "action": "MODIFY",
        "objectType": object_type,
        "accession": accession,
        "updates": updates,
    }


# ── TSV template generation ──────────────────────────────────────────────────

def generate_sample_template(checklist: str) -> list[dict[str, str]]:
    """Generate a sample metadata template as a list of column dicts.

    Returns a list with one row: column descriptions. The Marimo notebook
    uses this to pre-populate a dataframe editor.
    """
    info = ENA_CHECKLISTS.get(checklist, ENA_CHECKLISTS["ERC000011"])
    columns = ["sample_alias", "tax_id"] + info["required"] + [
        f for f in COMMON_OPTIONAL if f not in info["required"] and f not in ("sample_alias", "tax_id")
    ]
    descriptions: dict[str, str] = {
        "sample_alias": "Unique name for this sample",
        "tax_id": "NCBI taxonomy ID (e.g. 408169 for metagenome)",
        "scientific_name": "Scientific name matching tax_id",
        "collection date": "YYYY-MM-DD or YYYY-MM or YYYY",
        "geographic location (country and/or sea)": "Country or sea name",
        "geographic location (latitude)": "Decimal degrees N/S (e.g. 47.6 N)",
        "geographic location (longitude)": "Decimal degrees E/W (e.g. 122.3 W)",
        "broad-scale environmental context": "ENVO biome term",
        "local-scale environmental context": "ENVO local environment term",
        "environmental medium": "ENVO material term",
        "depth": "Depth in meters",
        "elevation": "Elevation in meters",
        "host scientific name": "Host organism scientific name",
        "sample_title": "Short title for the sample",
        "sample_description": "Free-text description",
        "isolation_source": "Source from which sample was isolated",
        "temperature": "Temperature in Celsius",
        "pH": "pH value",
        "salinity": "Salinity (PSU or ppt)",
    }
    row = {col: descriptions.get(col, "") for col in columns}
    return [row]


async def generate_template_from_ena(checklist_id: str) -> list[dict[str, str]] | None:
    """Generate a template by fetching live field defs from ENA.

    Returns None if the fetch fails (caller should fall back to hardcoded).
    """
    parsed = await fetch_checklist_fields(checklist_id)
    if not parsed:
        return None

    columns = ["sample_alias", "tax_id"]
    for field_name, field_def in parsed["fields"].items():
        if field_name not in columns:
            if field_def["mandatory"]:
                columns.append(field_name)
    # Then optional fields
    for field_name, field_def in parsed["fields"].items():
        if field_name not in columns and not field_def["mandatory"]:
            columns.append(field_name)

    row = {}
    for col in columns:
        field_def = parsed["fields"].get(col, {})
        desc = field_def.get("description", "")
        if field_def.get("enum_values"):
            desc += f" (options: {', '.join(field_def['enum_values'][:5])})"
        if field_def.get("units"):
            desc += f" (units: {', '.join(field_def['units'][:3])})"
        row[col] = desc
    row.setdefault("sample_alias", "Unique name for this sample")
    row.setdefault("tax_id", "NCBI taxonomy ID")
    return [row]


# ── Validation ───────────────────────────────────────────────────────────────

def validate_for_ena(
    rows: list[dict[str, Any]],
    checklist: str,
) -> dict[str, Any]:
    """Validate metadata rows against a hardcoded ENA checklist.

    Returns {"valid": bool, "errors": [...], "warnings": [...]}.
    For live validation against ENA's actual fields, use validate_against_checklist().
    """
    info = ENA_CHECKLISTS.get(checklist)
    if not info:
        return {"valid": False, "errors": [f"Unknown checklist: {checklist}"], "warnings": []}

    errors: list[str] = []
    warnings: list[str] = []

    if not rows:
        return {"valid": False, "errors": ["No sample rows provided"], "warnings": []}

    required = set(info["required"])

    for i, row in enumerate(rows, 1):
        alias = row.get("sample_alias", row.get("alias", f"row {i}"))
        for field in required:
            val = row.get(field)
            if val is None or (isinstance(val, str) and not val.strip()):
                errors.append(f"Sample '{alias}': missing required field '{field}'")

        tax_id = row.get("tax_id")
        if tax_id is not None and str(tax_id).strip():
            try:
                int(str(tax_id).strip())
            except ValueError:
                errors.append(f"Sample '{alias}': tax_id must be numeric, got '{tax_id}'")

        cdate = row.get("collection date", "")
        if cdate and not re.match(r"^\d{4}(-\d{2}(-\d{2})?)?$", str(cdate).strip()):
            warnings.append(
                f"Sample '{alias}': collection date '{cdate}' should be YYYY, YYYY-MM, or YYYY-MM-DD"
            )

        if not row.get("sample_alias") and not row.get("alias"):
            warnings.append(f"Row {i}: no sample_alias — ENA will auto-generate one")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── FTP instructions ─────────────────────────────────────────────────────────

def generate_ftp_instructions(webin_id: str, files: list[str]) -> str:
    """Generate copy-paste FTP upload commands for ENA."""
    lines = [
        "# Upload files to ENA via FTP",
        f"# Webin account: {webin_id}",
        "",
        "# Option 1: lftp (recommended for large files)",
        f"lftp -u {webin_id} webin2.ebi.ac.uk << 'EOF'",
    ]
    for f in files:
        lines.append(f"put {f}")
    lines.extend([
        "bye",
        "EOF",
        "",
        "# Option 2: curl (one file at a time)",
    ])
    for f in files:
        lines.append(
            f"curl -T {f} ftp://webin2.ebi.ac.uk/ --user {webin_id}"
        )
    lines.extend([
        "",
        "# Generate MD5 checksums (needed for submission)",
    ])
    for f in files:
        lines.append(f"md5sum {f}")
    return "\n".join(lines)
