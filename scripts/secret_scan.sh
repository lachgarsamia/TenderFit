#!/usr/bin/env bash
# Scans the working tree and the whole Git history with Gitleaks. Findings are redacted. Fails if anything is found outside dependencies.
set -uo pipefail
OUT=${OUT:-$(mktemp -d)}
chmod 777 "$OUT"  # the scanner container runs as its own user
IMAGE=zricethezav/gitleaks:v8.18.4
docker run --rm -v "$PWD:/repo:ro" -v "$OUT:/out" "$IMAGE" detect --source /repo --report-format json --report-path /out/history.json --redact --no-banner --exit-code 0 2>&1 | tail -1
docker run --rm -v "$PWD:/repo:ro" -v "$OUT:/out" "$IMAGE" detect --no-git --source /repo --report-format json --report-path /out/tree.json --redact --no-banner --exit-code 0 2>&1 | tail -1
python3 - "$OUT" <<'PY'
import json, sys
from collections import Counter
bad = 0
for name in ("history", "tree"):
    found = json.load(open(f"{sys.argv[1]}/{name}.json"))
    own = [f for f in found if not any(s in f["File"] for s in ("node_modules", ".venv", "__pycache__", ".mypy_cache", "web/dist"))]
    print(f"{name}: {len(found)} raw findings, {len(own)} outside dependencies and caches")
    for (rule, file), n in Counter((f["RuleID"], f["File"]) for f in own).most_common():
        print(f"  {rule}  {file}  x{n}")
    bad += len(own)
sys.exit(1 if bad else 0)
PY
