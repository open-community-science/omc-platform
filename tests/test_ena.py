"""Tests for ENA submission helpers — field mapping, XML, validation, receipts.

All tests are fast unit tests (no network, no LLM).
"""

import xml.etree.ElementTree as ET

import pytest
from ai.ena_submission import (
    convert_ncbi_to_ena,
    convert_ena_to_ncbi,
    resolve_checklist,
    build_project_payload,
    build_samples_payload,
    build_experiment_run_payload,
    build_modify_payload,
    build_project_xml,
    build_sample_xml,
    build_experiment_xml,
    build_run_xml,
    build_submission_xml,
    parse_receipt_xml,
    parse_checklist_xml,
    get_checklist_required_fields,
    validate_against_checklist,
    generate_sample_template,
    validate_for_ena,
    generate_ftp_instructions,
    _parse_lat_lon,
    _xml_to_bytes,
    ENA_CHECKLISTS,
)


# ── Field mapping ─────────────────────────────────────────────────────────────

class TestFieldMapping:
    def test_ncbi_to_ena_basic(self):
        row = {
            "sample_name": "sample1",
            "organism": "metagenome",
            "collection_date": "2025-06-15",
            "geo_loc_name": "Canada: British Columbia",
        }
        result = convert_ncbi_to_ena(row)
        assert result["sample_alias"] == "sample1"
        assert result["scientific_name"] == "metagenome"
        assert result["collection date"] == "2025-06-15"
        assert result["geographic location (country and/or sea)"] == "Canada: British Columbia"

    def test_ncbi_to_ena_lat_lon_split(self):
        row = {"lat_lon": "47.6 N 122.3 W"}
        result = convert_ncbi_to_ena(row)
        assert result["geographic location (latitude)"] == "47.6 N"
        assert result["geographic location (longitude)"] == "122.3 W"

    def test_ncbi_to_ena_lat_lon_south_east(self):
        row = {"lat_lon": "33.9 S 151.2 E"}
        result = convert_ncbi_to_ena(row)
        assert result["geographic location (latitude)"] == "33.9 S"
        assert result["geographic location (longitude)"] == "151.2 E"

    def test_ncbi_to_ena_skips_empty(self):
        row = {"sample_name": "s1", "organism": "", "host": None, "depth": "10"}
        result = convert_ncbi_to_ena(row)
        assert "scientific_name" not in result
        assert "host scientific name" not in result
        assert result["depth"] == "10"

    def test_ena_to_ncbi_basic(self):
        row = {
            "sample_alias": "sample1",
            "scientific_name": "metagenome",
            "collection date": "2025-06-15",
        }
        result = convert_ena_to_ncbi(row)
        assert result["sample_name"] == "sample1"
        assert result["organism"] == "metagenome"
        assert result["collection_date"] == "2025-06-15"

    def test_ena_to_ncbi_lat_lon_merge(self):
        row = {
            "geographic location (latitude)": "47.6 N",
            "geographic location (longitude)": "122.3 W",
        }
        result = convert_ena_to_ncbi(row)
        assert result["lat_lon"] == "47.6 N 122.3 W"

    def test_roundtrip_preserves_unknown_fields(self):
        row = {"sample_name": "s1", "custom_field": "value"}
        ena = convert_ncbi_to_ena(row)
        assert ena["custom_field"] == "value"


class TestParseLatLon:
    def test_standard(self):
        assert _parse_lat_lon("47.6 N 122.3 W") == ("47.6 N", "122.3 W")

    def test_lowercase(self):
        lat, lon = _parse_lat_lon("47.6 n 122.3 w")
        assert lat == "47.6 N"
        assert lon == "122.3 W"

    def test_bad_format_returns_input(self):
        lat, lon = _parse_lat_lon("not a coordinate")
        assert lat == "not a coordinate"
        assert lon == ""


# ── Checklist resolution ──────────────────────────────────────────────────────

class TestChecklistResolution:
    def test_water(self):
        assert resolve_checklist("water") == "ERC000024"

    def test_marine(self):
        assert resolve_checklist("marine environment") == "ERC000024"

    def test_soil(self):
        assert resolve_checklist("soil") == "ERC000014"

    def test_sediment(self):
        assert resolve_checklist("deep sea sediment") == "ERC000028"

    def test_host_associated(self):
        assert resolve_checklist("host-associated") == "ERC000013"

    def test_human(self):
        assert resolve_checklist("human gut") == "ERC000052"

    def test_default(self):
        assert resolve_checklist("something unknown") == "ERC000011"


# ── Dict payload builders (legacy) ───────────────────────────────────────────

class TestPayloadBuilders:
    def test_project_payload(self):
        p = build_project_payload("my-study", "My Title", "A description", hold_date="2026-01-01")
        assert p["alias"] == "my-study"
        assert p["title"] == "My Title"
        assert p["description"] == "A description"
        assert p["studyType"] == "Metagenomics"
        assert p["holdDate"] == "2026-01-01"

    def test_project_payload_no_hold(self):
        p = build_project_payload("s1", "Title", "Desc")
        assert "holdDate" not in p

    def test_samples_payload(self):
        samples = [
            {
                "sample_alias": "sample1",
                "tax_id": "408169",
                "scientific_name": "metagenome",
                "collection date": "2025-06",
                "depth": "5m",
            }
        ]
        p = build_samples_payload(samples, "ERC000024")
        assert p["action"] == "ADD"
        assert len(p["samples"]) == 1
        s = p["samples"][0]
        assert s["alias"] == "sample1"
        assert s["checklist"] == "ERC000024"
        assert s["taxId"] == 408169
        assert s["scientificName"] == "metagenome"
        assert "collection date" in s["attributes"]
        assert s["attributes"]["collection date"][0]["value"] == "2025-06"

    def test_samples_payload_modify_action(self):
        p = build_samples_payload([{"alias": "s1"}], "ERC000011", action="MODIFY")
        assert p["action"] == "MODIFY"

    def test_experiment_run_payload(self):
        exp = [{
            "study_accession": "ERP123456",
            "sample_accession": "ERS654321",
            "library_strategy": "WGS",
            "library_source": "METAGENOMIC",
            "library_selection": "RANDOM",
            "platform": "OXFORD_NANOPORE",
            "instrument": "MinION",
            "files": [
                {"filename": "reads.fastq.gz", "filetype": "fastq", "checksum_method": "MD5", "checksum": "abc123"},
            ],
        }]
        p = build_experiment_run_payload(exp)
        assert len(p["experiments"]) == 1
        e = p["experiments"][0]
        assert e["studyRef"] == "ERP123456"
        assert e["sampleRef"] == "ERS654321"
        assert e["libraryDescriptor"]["libraryStrategy"] == "WGS"
        assert len(e["runs"]) == 1
        assert e["runs"][0]["filename"] == "reads.fastq.gz"

    def test_modify_payload(self):
        p = build_modify_payload("sample", "ERS123", {"title": "New title"})
        assert p["action"] == "MODIFY"
        assert p["objectType"] == "sample"
        assert p["accession"] == "ERS123"
        assert p["updates"]["title"] == "New title"


# ── XML builders ──────────────────────────────────────────────────────────────

class TestXMLBuilders:
    def test_submission_xml_add(self):
        xml_bytes = build_submission_xml(action="ADD")
        root = ET.fromstring(xml_bytes)
        assert root.tag == "SUBMISSION"
        actions = root.findall(".//ACTION")
        assert len(actions) == 1
        assert actions[0].find("ADD") is not None

    def test_submission_xml_with_hold_date(self):
        xml_bytes = build_submission_xml(action="ADD", hold_date="2027-01-01")
        root = ET.fromstring(xml_bytes)
        actions = root.findall(".//ACTION")
        assert len(actions) == 2
        hold = actions[1].find("HOLD")
        assert hold is not None
        assert hold.get("HoldUntilDate") == "2027-01-01"

    def test_submission_xml_validate(self):
        xml_bytes = build_submission_xml(action="VALIDATE")
        root = ET.fromstring(xml_bytes)
        assert root.find(".//VALIDATE") is not None

    def test_project_xml(self):
        xml_bytes = build_project_xml("my-study", "My Title", "A great study")
        root = ET.fromstring(xml_bytes)
        assert root.tag == "PROJECT_SET"
        project = root.find("PROJECT")
        assert project.get("alias") == "my-study"
        assert project.find("TITLE").text == "My Title"
        assert project.find("DESCRIPTION").text == "A great study"
        assert project.find(".//SEQUENCING_PROJECT") is not None

    def test_sample_xml(self):
        samples = [{
            "sample_alias": "sample1",
            "tax_id": "408169",
            "scientific_name": "metagenome",
            "collection date": "2025-06",
            "depth": "5m",
        }]
        xml_bytes = build_sample_xml(samples, "ERC000024")
        root = ET.fromstring(xml_bytes)
        assert root.tag == "SAMPLE_SET"
        sample = root.find("SAMPLE")
        assert sample.get("alias") == "sample1"
        assert sample.find(".//TAXON_ID").text == "408169"
        assert sample.find(".//SCIENTIFIC_NAME").text == "metagenome"
        # Check ENA-CHECKLIST attribute
        attrs = sample.findall(".//SAMPLE_ATTRIBUTE")
        tags = {a.find("TAG").text: a.find("VALUE").text for a in attrs}
        assert tags["ENA-CHECKLIST"] == "ERC000024"
        assert tags["collection date"] == "2025-06"
        assert tags["depth"] == "5m"

    def test_sample_xml_multiple(self):
        samples = [
            {"sample_alias": "s1", "scientific_name": "metagenome"},
            {"sample_alias": "s2", "scientific_name": "soil metagenome"},
        ]
        xml_bytes = build_sample_xml(samples, "ERC000011")
        root = ET.fromstring(xml_bytes)
        assert len(root.findall("SAMPLE")) == 2

    def test_experiment_xml(self):
        exps = [{
            "alias": "exp1",
            "study_accession": "PRJEB99999",
            "sample_accession": "ERS123456",
            "library_strategy": "WGS",
            "library_source": "METAGENOMIC",
            "library_selection": "RANDOM",
            "platform": "OXFORD_NANOPORE",
            "instrument": "MinION",
        }]
        xml_bytes = build_experiment_xml(exps)
        root = ET.fromstring(xml_bytes)
        assert root.tag == "EXPERIMENT_SET"
        exp = root.find("EXPERIMENT")
        assert exp.get("alias") == "exp1"
        assert exp.find("STUDY_REF").get("accession") == "PRJEB99999"
        assert exp.find(".//SAMPLE_DESCRIPTOR").get("accession") == "ERS123456"
        assert exp.find(".//LIBRARY_STRATEGY").text == "WGS"
        assert exp.find(".//INSTRUMENT_MODEL").text == "MinION"

    def test_run_xml(self):
        runs = [{
            "alias": "run1",
            "experiment_alias": "exp1",
            "files": [
                {"filename": "reads.fastq.gz", "filetype": "fastq", "checksum_method": "MD5", "checksum": "abc123"},
            ],
        }]
        xml_bytes = build_run_xml(runs)
        root = ET.fromstring(xml_bytes)
        assert root.tag == "RUN_SET"
        run = root.find("RUN")
        assert run.get("alias") == "run1"
        assert run.find("EXPERIMENT_REF").get("refname") == "exp1"
        f = run.find(".//FILE")
        assert f.get("filename") == "reads.fastq.gz"
        assert f.get("checksum") == "abc123"

    def test_run_xml_with_accession(self):
        runs = [{
            "alias": "run1",
            "experiment_accession": "ERX999999",
            "files": [{"filename": "f.fq.gz"}],
        }]
        xml_bytes = build_run_xml(runs)
        root = ET.fromstring(xml_bytes)
        assert root.find(".//EXPERIMENT_REF").get("accession") == "ERX999999"


# ── Receipt parsing ───────────────────────────────────────────────────────────

class TestReceiptParsing:
    def test_success_receipt(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <RECEIPT receiptDate="2026-03-20" success="true">
            <PROJECT alias="my-study" accession="PRJEB99999" />
            <SUBMISSION alias="sub1" accession="ERA1234567" />
            <ACTIONS>ADD</ACTIONS>
            <INFO>Submission has been committed.</INFO>
        </RECEIPT>"""
        result = parse_receipt_xml(xml)
        assert result["success"] is True
        assert result["accessions"]["my-study"] == "PRJEB99999"
        assert "Submission has been committed." in result["messages"]["info"]

    def test_failure_receipt(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <RECEIPT receiptDate="2026-03-20" success="false">
            <ERROR>Missing mandatory field: collection date</ERROR>
            <ERROR>Invalid taxon ID: not-a-number</ERROR>
        </RECEIPT>"""
        result = parse_receipt_xml(xml)
        assert result["success"] is False
        assert len(result["messages"]["error"]) == 2
        assert "collection date" in result["messages"]["error"][0]

    def test_sample_receipt_with_ext_id(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <RECEIPT success="true">
            <SAMPLE alias="s1" accession="ERS123456">
                <EXT_ID type="biosample" accession="SAMEA12345678" />
            </SAMPLE>
        </RECEIPT>"""
        result = parse_receipt_xml(xml)
        assert result["success"] is True
        assert result["accessions"]["s1"] == "ERS123456"
        assert result["accessions"]["s1:biosample"] == "SAMEA12345678"

    def test_multiple_accessions(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <RECEIPT success="true">
            <SAMPLE alias="s1" accession="ERS111" />
            <SAMPLE alias="s2" accession="ERS222" />
            <SAMPLE alias="s3" accession="ERS333" />
        </RECEIPT>"""
        result = parse_receipt_xml(xml)
        assert len(result["accessions"]) == 3
        assert result["accessions"]["s2"] == "ERS222"

    def test_invalid_xml(self):
        result = parse_receipt_xml("not xml at all")
        assert result["success"] is False
        assert len(result["messages"]["error"]) > 0

    def test_empty_receipt(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <RECEIPT success="false"></RECEIPT>"""
        result = parse_receipt_xml(xml)
        assert result["success"] is False
        assert result["accessions"] == {}


# ── Checklist XML parsing ─────────────────────────────────────────────────────

SAMPLE_CHECKLIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CHECKLIST_SET>
  <CHECKLIST accession="ERC000011">
    <DESCRIPTOR>
      <NAME>ENA default sample checklist</NAME>
      <DESCRIPTION>Minimum info for any ENA sample.</DESCRIPTION>
    </DESCRIPTOR>
    <FIELD>
      <LABEL>collection date</LABEL>
      <DESCRIPTION>Date of sample collection</DESCRIPTION>
      <MANDATORY>mandatory</MANDATORY>
      <FIELD_TYPE>
        <TEXT_FIELD>
          <REGEX_VALUE>^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$</REGEX_VALUE>
        </TEXT_FIELD>
      </FIELD_TYPE>
    </FIELD>
    <FIELD>
      <LABEL>geographic location (country and/or sea)</LABEL>
      <DESCRIPTION>Country or sea where sample was collected</DESCRIPTION>
      <MANDATORY>mandatory</MANDATORY>
      <FIELD_TYPE>
        <TEXT_CHOICE_FIELD>
          <TEXT_VALUE><VALUE>Afghanistan</VALUE></TEXT_VALUE>
          <TEXT_VALUE><VALUE>Canada</VALUE></TEXT_VALUE>
          <TEXT_VALUE><VALUE>United Kingdom</VALUE></TEXT_VALUE>
        </TEXT_CHOICE_FIELD>
      </FIELD_TYPE>
    </FIELD>
    <FIELD>
      <LABEL>depth</LABEL>
      <DESCRIPTION>Depth of sample collection</DESCRIPTION>
      <MANDATORY>optional</MANDATORY>
      <FIELD_TYPE>
        <TEXT_FIELD/>
      </FIELD_TYPE>
      <UNITS>
        <UNIT>m</UNIT>
        <UNIT>cm</UNIT>
      </UNITS>
    </FIELD>
  </CHECKLIST>
</CHECKLIST_SET>"""


class TestChecklistParsing:
    def test_parse_basic(self):
        result = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        assert result["checklist_id"] == "ERC000011"
        assert result["name"] == "ENA default sample checklist"
        assert "collection date" in result["fields"]
        assert "depth" in result["fields"]

    def test_mandatory_fields(self):
        result = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        assert result["fields"]["collection date"]["mandatory"] is True
        assert result["fields"]["depth"]["mandatory"] is False

    def test_regex_field(self):
        result = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        f = result["fields"]["collection date"]
        assert f["type"] == "regex"
        assert f["regex"] is not None
        assert "^[0-9]{4}" in f["regex"]

    def test_choice_field(self):
        result = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        f = result["fields"]["geographic location (country and/or sea)"]
        assert f["type"] == "choice"
        assert "Canada" in f["enum_values"]

    def test_units(self):
        result = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        f = result["fields"]["depth"]
        assert f["units"] == ["m", "cm"]

    def test_get_required_fields(self):
        parsed = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        required = get_checklist_required_fields(parsed)
        assert "collection date" in required
        assert "geographic location (country and/or sea)" in required
        assert "depth" not in required


# ── Live checklist validation ─────────────────────────────────────────────────

class TestLiveChecklistValidation:
    def test_valid_against_parsed(self):
        parsed = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        row = {
            "collection date": "2025-06-15",
            "geographic location (country and/or sea)": "Canada",
            "depth": "5",
        }
        result = validate_against_checklist([row], parsed)
        assert result["valid"] is True

    def test_missing_mandatory(self):
        parsed = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        row = {"depth": "5"}  # missing collection date and geo
        result = validate_against_checklist([row], parsed)
        assert result["valid"] is False
        assert any("collection date" in e for e in result["errors"])

    def test_regex_mismatch_warns(self):
        parsed = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        row = {
            "collection date": "June 2025",  # bad format
            "geographic location (country and/or sea)": "Canada",
        }
        result = validate_against_checklist([row], parsed)
        assert any("collection date" in w for w in result["warnings"])

    def test_enum_mismatch_warns(self):
        parsed = parse_checklist_xml(SAMPLE_CHECKLIST_XML)
        row = {
            "collection date": "2025",
            "geographic location (country and/or sea)": "Narnia",
        }
        result = validate_against_checklist([row], parsed)
        assert any("Narnia" in w for w in result["warnings"])


# ── Template generation ───────────────────────────────────────────────────────

class TestTemplateGeneration:
    def test_water_template_columns(self):
        rows = generate_sample_template("ERC000024")
        assert len(rows) == 1
        cols = list(rows[0].keys())
        assert "sample_alias" in cols
        assert "tax_id" in cols
        assert "depth" in cols
        assert "geographic location (latitude)" in cols

    def test_default_template(self):
        rows = generate_sample_template("ERC000011")
        cols = list(rows[0].keys())
        assert "scientific_name" in cols
        assert "collection date" in cols

    def test_unknown_checklist_falls_back(self):
        rows = generate_sample_template("UNKNOWN")
        assert len(rows) == 1


# ── Validation (hardcoded) ───────────────────────────────────────────────────

class TestValidation:
    def test_valid_water_sample(self):
        row = {
            "sample_alias": "s1",
            "scientific_name": "metagenome",
            "collection date": "2025-06-15",
            "geographic location (country and/or sea)": "Canada",
            "geographic location (latitude)": "49.2 N",
            "geographic location (longitude)": "123.1 W",
            "broad-scale environmental context": "marine biome",
            "local-scale environmental context": "coastal water",
            "environmental medium": "sea water",
            "depth": "5m",
        }
        result = validate_for_ena([row], "ERC000024")
        assert result["valid"] is True
        assert len(result["errors"]) == 0

    def test_missing_required_fields(self):
        row = {"sample_alias": "s1", "scientific_name": "metagenome"}
        result = validate_for_ena([row], "ERC000024")
        assert result["valid"] is False
        assert any("collection date" in e for e in result["errors"])

    def test_bad_tax_id(self):
        row = {
            "sample_alias": "s1",
            "tax_id": "not-a-number",
            "scientific_name": "metagenome",
            "collection date": "2025",
        }
        result = validate_for_ena([row], "ERC000011")
        assert any("tax_id" in e for e in result["errors"])

    def test_bad_date_format_warns(self):
        row = {
            "sample_alias": "s1",
            "scientific_name": "metagenome",
            "collection date": "June 2025",
        }
        result = validate_for_ena([row], "ERC000011")
        assert any("collection date" in w for w in result["warnings"])

    def test_empty_rows(self):
        result = validate_for_ena([], "ERC000011")
        assert result["valid"] is False

    def test_unknown_checklist(self):
        result = validate_for_ena([{"sample_alias": "s1"}], "FAKE")
        assert result["valid"] is False
        assert any("Unknown checklist" in e for e in result["errors"])

    def test_missing_alias_warns(self):
        row = {
            "scientific_name": "metagenome",
            "collection date": "2025",
        }
        result = validate_for_ena([row], "ERC000011")
        assert any("sample_alias" in w for w in result["warnings"])


# ── FTP instructions ──────────────────────────────────────────────────────────

class TestFTPInstructions:
    def test_generates_commands(self):
        result = generate_ftp_instructions("Webin-12345", ["reads.fastq.gz", "reads2.fastq.gz"])
        assert "Webin-12345" in result
        assert "reads.fastq.gz" in result
        assert "reads2.fastq.gz" in result
        assert "lftp" in result
        assert "curl" in result
        assert "md5sum" in result

    def test_single_file(self):
        result = generate_ftp_instructions("Webin-99", ["one.fq.gz"])
        assert result.count("one.fq.gz") >= 3  # lftp, curl, md5sum
