"""Write the API contract to docs/openapi.json (PYTHONPATH=. python scripts/export_openapi.py). tests/test_api_contract.py fails when it is out of date."""
import json
from pathlib import Path

from tenderfit.api.main import app

OUT = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"

if __name__ == "__main__":
    OUT.write_text(json.dumps(app.openapi(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT}")
