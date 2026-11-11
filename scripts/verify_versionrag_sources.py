"""Verify the frozen FTS projections against the publisher's original XML archives."""
import argparse
import hashlib
import io
import json
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1] / "eval/real/fts-2023"


def project(content: bytes) -> dict:
    tree = ET.fromstring(content)
    for node in tree.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    form = next(node for node in tree.findall("FORM_SECTION/*") if node.get("FORM"))

    def value(node, path):
        return " ".join(" ".join(child.itertext()).strip() for child in node.findall(path)).strip()

    return {
        "form": form.tag, "title": value(form, "OBJECT_CONTRACT/TITLE"), "sector": value(tree, ".//ORIGINAL_CPV"),
        "submission": value(form, "PROCEDURE/DATE_RECEIPT_TENDERS"),
        "opening": value(form, "PROCEDURE/OPENING_CONDITION/DATE_OPENING_TENDERS"),
        "dispatch": value(form, "COMPLEMENTARY_INFO/DATE_DISPATCH_NOTICE"),
        "original_reference": value(form, "COMPLEMENTARY_INFO/NOTICE_NUMBER_OJ"),
        "changes": [{"where": value(c, "WHERE/SECTION"), "lot": value(c, "WHERE/LOT_NO"), "label": value(c, "WHERE/LABEL"),
                     "old": value(c, "OLD_VALUE"), "new": value(c, "NEW_VALUE")} for c in form.findall("CHANGES/CHANGE")],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml-dir", type=Path, help="read previously downloaded XML files by notice ID")
    args = parser.parse_args()
    sources = json.loads((ROOT / "sources.json").read_text())
    archives: dict[str, bytes] = {}
    for notice, record in sources.items():
        if args.xml_dir:
            raw = (args.xml_dir / record["filename"]).read_bytes()
        else:
            url = record["archive"]
            if url not in archives:
                with urllib.request.urlopen(url, timeout=45) as response:
                    archives[url] = response.read()
            with zipfile.ZipFile(io.BytesIO(archives[url])) as zipped:
                [member] = [n for n in zipped.namelist() if Path(n).name == record["filename"]]
                raw = zipped.read(member)
        if hashlib.sha256(raw).hexdigest() != record["sha256"] or any(record[k] != v for k, v in project(raw).items()):
            raise ValueError(f"source mismatch for {notice}")
    print(f"Verified hashes and projected fields for {len(sources)} original XML files.")


if __name__ == "__main__":
    main()
