#!/bin/zsh
# Install (or refresh) the evaluation launchd agents for this checkout:
#   com.provenant.nightly-eval  attribution accuracy  (config/eval/attribution.yaml nightly.start_utc)
#   com.provenant.daily-eval    attack success, utility (config/eval/security.yaml daily.start_utc)
# Remove one with: launchctl bootout gui/$(id -u)/<label> && rm ~/Library/LaunchAgents/<label>.plist
set -e
repo="${0:A:h:h:h}"
install_one() {
  local label=$1 yaml=$2 key=$3
  local utc=$(grep -E "^\s*$key:" "$repo/config/eval/$yaml" | head -1 | sed -E 's/.*"([0-9]+):([0-9]+)".*/\1 \2/')
  local uh um lh lm
  read uh um <<< "$utc"
  read lh lm <<< "$(date -j -u -f "%H:%M" "$uh:$um" "+%s" | xargs -I{} date -r {} "+%H %M")"
  local dest=~/Library/LaunchAgents/$label.plist
  sed -e "s|__REPO__|$repo|" -e "s|__HOUR__|$((10#$lh))|" -e "s|__MINUTE__|$((10#$lm))|" \
    "$repo/scripts/launchd/$label.plist.template" > "$dest"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dest"
  echo "installed $label: daily at $lh:$lm local ($uh:$um UTC)"
}
install_one com.provenant.nightly-eval attribution.yaml start_utc
install_one com.provenant.daily-eval security.yaml start_utc
