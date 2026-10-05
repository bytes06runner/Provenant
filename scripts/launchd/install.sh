#!/bin/zsh
# Install (or refresh) the nightly eval launchd agent for this checkout.
# Remove with: launchctl bootout gui/$(id -u)/com.provenant.nightly-eval && rm ~/Library/LaunchAgents/com.provenant.nightly-eval.plist
set -e
repo="${0:A:h:h:h}"
utc=$(grep -E '^\s*start_utc:' "$repo/config/eval/attribution.yaml" | sed -E 's/.*"([0-9]+):([0-9]+)".*/\1 \2/')
read uh um <<< "$utc"
# convert the UTC start time to local time
local_hm=$(date -j -u -f "%H:%M" "$uh:$um" "+%s" | xargs -I{} date -r {} "+%H %M")
read lh lm <<< "$local_hm"
dest=~/Library/LaunchAgents/com.provenant.nightly-eval.plist
sed -e "s|__REPO__|$repo|" -e "s|__HOUR__|$((10#$lh))|" -e "s|__MINUTE__|$((10#$lm))|" \
  "$repo/scripts/launchd/com.provenant.nightly-eval.plist.template" > "$dest"
launchctl bootout "gui/$(id -u)/com.provenant.nightly-eval" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$dest"
echo "installed: daily at $lh:$lm local ($uh:$um UTC) -> $dest"
