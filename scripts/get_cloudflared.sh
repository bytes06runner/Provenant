#!/usr/bin/env bash
# Download cloudflared for macOS arm64 into bin/ and verify it before use.
#
# Two official checksums are checked, because they cover different files:
#   * GitHub's asset digest is the SHA256 of the .tgz archive
#   * the release notes list the SHA256 of the cloudflared binary inside it
# The binary's Apple Developer ID signature (Cloudflare, team 68WVV388M8) is checked too.
#
# Usage: scripts/get_cloudflared.sh [version]   (default: latest release)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASSET="cloudflared-darwin-arm64.tgz"
TEAM_ID="68WVV388M8"
VERSION="${1:-latest}"

if [[ "$VERSION" == "latest" ]]; then
  API="https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
else
  API="https://api.github.com/repos/cloudflare/cloudflared/releases/tags/${VERSION}"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

curl -fsSL "$API" -o "$WORK/release.json"
read -r TAG URL TGZ_SHA BIN_SHA < <(python3 - "$WORK/release.json" "$ASSET" <<'EOF'
import json, re, sys
release = json.load(open(sys.argv[1]))
asset = next(a for a in release["assets"] if a["name"] == sys.argv[2])
tgz_sha = asset["digest"].removeprefix("sha256:")
m = re.search(re.escape(sys.argv[2]) + r":\s*([0-9a-f]{64})", release["body"])
if not m:
    sys.exit("binary checksum not found in release notes")
print(release["tag_name"], asset["browser_download_url"], tgz_sha, m.group(1))
EOF
)

echo "cloudflared ${TAG}"
curl -fsSL "$URL" -o "$WORK/$ASSET"
echo "${TGZ_SHA}  $WORK/$ASSET" | shasum -a 256 -c -
tar -xzf "$WORK/$ASSET" -C "$WORK"
echo "${BIN_SHA}  $WORK/cloudflared" | shasum -a 256 -c -
codesign --verify --strict "$WORK/cloudflared"
codesign -dv "$WORK/cloudflared" 2>&1 | grep -q "TeamIdentifier=${TEAM_ID}" \
  || { echo "unexpected signing team" >&2; exit 1; }

mkdir -p "$REPO_ROOT/bin"
install -m 0755 "$WORK/cloudflared" "$REPO_ROOT/bin/cloudflared"
"$REPO_ROOT/bin/cloudflared" --version
