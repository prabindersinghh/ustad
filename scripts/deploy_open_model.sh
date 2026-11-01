#!/usr/bin/env bash
#
# Deploy a Meta open-weight model on Modal and print the env vars Ustad needs.
#
# Why: the hosted Meta Model API is the fast path, but a factory's first
# question is whether its line footage leaves the site. Running the
# verification reasoning on a Meta open-weight model we deploy ourselves
# answers that, and makes the build eligible for the Best Build on Meta Open
# Weight Models prize.
#
# Muse Glimmer 30B is the right target: it fits comfortably in the hackathon
# credit allowance and is built for agentic tool calling, which is exactly
# what Ustad's verification loop does. Muse Spark Open is 816B MoE -- far too
# large for a $100 budget.
#
#   bash scripts/deploy_open_model.sh
#
set -euo pipefail

MODEL="${OPEN_MODEL:-meta-models/Muse-Glimmer-30B}"

command -v modal >/dev/null 2>&1 || {
  echo "modal CLI not found. Install it first:"
  echo "    pip install modal && python3 -m modal setup"
  exit 1
}

echo "==> Creating a dedicated endpoint for ${MODEL}"
echo "    (dedicated scales to zero, so idle time costs nothing)"
modal endpoint create --model "${MODEL}"

echo
echo "==> Creating a proxy token"
echo "    Modal has no separate API key: join the token id and secret with a"
echo "    dot and use that as the OpenAI api_key."
modal workspace proxy-tokens create

cat <<'NOTE'

------------------------------------------------------------------------
Now put these in .env, using the endpoint URL from the dashboard link the
create command printed:

    REASONING_BACKEND=open
    OPEN_BASE_URL=https://<your-endpoint-url>/v1
    OPEN_API_KEY=wk-<token-id>.ws-<token-secret>
    OPEN_MODEL=meta-models/Muse-Glimmer-30B

Verify it is really serving before you rely on it:

    curl -s "$OPEN_BASE_URL/models" -H "Authorization: Bearer $OPEN_API_KEY"

Then confirm Ustad picked it up:

    curl -s localhost:8000/api/health | python3 -m json.tool

"reasoning_backend" should read "open". Every step event is tagged with the
backend that judged it, so /api/metrics gives you the evidence that the
open-weight model really ran the core workload.

Keep OPEN_FALLBACK=1 during the live demo. If the endpoint cold-starts or
stalls, Ustad silently falls back to the hosted API rather than leaving a
worker stranded mid-procedure -- and the event log still records honestly
which backend judged which step.
------------------------------------------------------------------------
NOTE
