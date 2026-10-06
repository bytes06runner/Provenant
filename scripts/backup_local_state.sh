#!/bin/zsh
# Back up everything that exists only on this machine (never in git) to a private folder.
#
#   scripts/backup_local_state.sh [destination]      (default: ~/Provenant-private-backup)
#
# Copies: .env (secrets), var/keys (merchant and user signing keys), var/registry.json,
# the recorder, ledger and LLM databases (consistent online copies via sqlite3 .backup),
# case PDFs, run logs and state, eval results, and spike evidence. Each run is a new dated
# snapshot; the folder is readable by this user only. Nothing here may ever be committed.
set -euo pipefail
repo="${0:A:h:h}"
dest_root="${1:-$HOME/Provenant-private-backup}"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dest="$dest_root/$stamp"
umask 077
mkdir -p "$dest/var"
cp -p "$repo/.env" "$dest/.env"
cp -Rp "$repo/var/keys" "$dest/var/keys"
cp -p "$repo/var/registry.json" "$dest/var/"
for db in provenant ledger llm; do
  [ -f "$repo/var/$db.db" ] && sqlite3 "$repo/var/$db.db" ".backup '$dest/var/$db.db'"
done
[ -d "$repo/var/cases" ] && cp -Rp "$repo/var/cases" "$dest/var/cases"
for f in "$repo"/var/*.txt "$repo"/var/*.log "$repo"/var/*.json "$repo"/var/*.sh; do
  [ -f "$f" ] && cp -p "$f" "$dest/var/"
done
[ -d "$repo/eval/results" ] && cp -Rp "$repo/eval/results" "$dest/eval-results"
[ -d "$repo/spikes/out" ] && cp -Rp "$repo/spikes/out" "$dest/spikes-out"
chmod -R go-rwx "$dest_root"
# Verify the recorder copy: every session's hash chain must still verify.
( cd "$repo" && .venv/bin/python - "$dest/var/provenant.db" <<'PY'
import sys
from sqlalchemy import create_engine, text
from blackbox.recorder import FlightRecorder
engine = create_engine(f"sqlite:///{sys.argv[1]}")
rec = FlightRecorder(engine)
with engine.connect() as c:
    sessions = [r[0] for r in c.execute(text("select distinct session_id from recorder_events"))]
for s in sessions:
    rec.verify(s)
print(f"recorder copy verified: {len(sessions)} sessions")
PY
)
# Keep the newest 14 snapshots.
snaps=("$dest_root"/*Z(N/On))   # dated snapshot folders, newest first
for old in "${snaps[@]:14}"; do rm -rf "$old"; done
echo "backup: $dest"
du -sh "$dest" | cut -f1
