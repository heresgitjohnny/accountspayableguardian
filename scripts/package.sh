#!/usr/bin/env bash
# Build a clean source bundle (zip) for upload to GitHub.
# Excludes build output, virtualenvs, caches and account-specific files
# (cdk-outputs.json, agentcore/state.json), and blanks personal values in cdk.json.
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="ap-guardian"
STAMP="$(date +%Y%m%d)"
OUT="dist/${NAME}-${STAMP}.zip"
STAGE="dist/stage/${NAME}"

rm -rf dist/stage && mkdir -p "$STAGE"
rsync -a \
  --exclude '.git/' --exclude '.venv/' --exclude 'dist/' --exclude 'build/' --exclude 'cdk.out/' \
  --exclude '.vscode/' --exclude 'node_modules/' --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' \
  --exclude '.$*.bkp' --exclude 'cdk-outputs.json' --exclude 'agentcore/state.json' \
  --exclude 'tests/out/' --exclude 'docs/stress_results.md' \
  --exclude 'docs/APGuardianAWSArchitecture.png' --exclude 'docs/data-flow-simple.drawio.png' \
  ./ "$STAGE/"

# Ship cdk.json with blank deployment settings.
python3 - "$STAGE/cdk.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for k in ("inbox_address", "po_from_address", "clerk_email", "cognito_domain_prefix"):
    d["context"][k] = ""
json.dump(d, open(p, "w"), indent=2); open(p, "a").write("\n")
PY

# Refuse to bundle anything that looks like an account ID, key or personal address.
if grep -rEn "AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|[0-9]{12}\.dkr|arn:aws:[a-z-]+:[a-z0-9-]*:[0-9]{12}:" "$STAGE" \
     --include='*' --exclude-dir='.git' | grep -v 'Aws\.\|{ACCOUNT}\|f"arn\|123456789012' ; then
  echo "Possible account-specific data found above. Fix before uploading." >&2; exit 1
fi

mkdir -p dist && rm -f "$OUT"
(cd dist/stage && zip -qr "../$(basename "$OUT")" "$NAME")
rm -rf dist/stage
echo "Created $OUT"
unzip -l "$OUT" | tail -1
