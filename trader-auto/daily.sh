#!/bin/bash
# One automatic paper day. Run from the repo root at about 15:20 New York time
# (20:20 UK). Keys come from ~/.trader-keys (chmod 600), never from this repo:
#   export APCA_API_KEY_ID=...      # Alpaca PAPER keys only
#   export APCA_API_SECRET_KEY=...
set -u
cd "$(dirname "$0")/.." || exit 1
[ -f "$HOME/.trader-keys" ] && . "$HOME/.trader-keys"
mkdir -p trader/journal
LOG="trader/journal/daily-$(date +%Y-%m).log"
{
  echo "=== $(date) ==="
  python3 trader-regime/regime.py now
  # re-run the gauntlet on the first trading day of each month
  if [ ! -f trader/state/validation.json ] || [ "$(date +%d)" -le 3 ] && \
     [ "$(date -r trader/state/validation.json +%Y-%m 2>/dev/null)" != "$(date +%Y-%m)" ]; then
    python3 trader-validate/validate.py run --universe default --strategy rsi_reversion
  fi
  python3 trader-auto/auto.py run ${DRY_RUN:+--dry-run}
  python3 trader-risk/breakers.py status
} >> "$LOG" 2>&1
