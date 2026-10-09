#!/usr/bin/env bash
# Nightly self-improvement loop. Cron (UTC): 15 0 * * *  /path/to/trading-agent/ops/nightly.sh
# Produces a report + a CANDIDATE schema. Promotion stays a human gate:
#   python -m tradeagent.review.nightly evaluate --candidate <file> --day <day>
#   python -m tradeagent.review.nightly promote  --candidate <file> --i-approve
set -euo pipefail
cd "$(dirname "$0")/.."
DAY=$(date -u -d "yesterday" +%F)
python -m tradeagent.review.nightly report --day "$DAY"
python -m tradeagent.research regime || echo "regime fetch failed (see MISSING)"
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  python -m tradeagent.review.nightly propose --day "$DAY"
fi
