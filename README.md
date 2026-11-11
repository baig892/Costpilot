# CostPilot v2

Read-only AWS cost agent. It scans your account, reads real spend from Cost Explorer,
writes a full report, and (optionally) posts an AI-written summary to Slack.
It never changes or deletes anything.

## What it checks
- **EC2, 90 days:** CPU (avg / p95 / peak) and network per instance, then a verdict:
  `idle`, `oversized` (with a one-size-down recommendation and estimated saving),
  `ok`, `hot` (running too hard), or `not_enough_data`.
- **Storage and network:** unattached EBS volumes, gp2 -> gp3, snapshots older than 90 days,
  unused Elastic IPs.
- **Other services:** idle RDS (zero connections), low-CPU RDS, idle NAT gateways,
  idle load balancers, CloudWatch log groups that never expire.
- **Real spend:** Cost Explorer, last 30 days vs the 30 days before, by service.

## Run it
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    export AWS_REGION=us-east-1            # or REGIONS="us-east-1,eu-west-1" or REGIONS=all
    export ANTHROPIC_API_KEY=...           # optional: AI summary
    export SLACK_WEBHOOK_URL=...           # optional: post to Slack
    python costpilot.py

Output goes to `./reports/`: `costpilot-DATE.md` (read this first), `instances-DATE.csv`
(open in Excel), `findings-DATE.json`, and `history.csv` (one line per run, to track progress).

Check the logic without AWS: `python test_logic.py`

## IAM
Attach `iam-policy.json` to the user or role that runs it. Read-only on purpose.

## Good to know
- **Memory is not measured** (needs the CloudWatch agent). Low CPU does not always mean oversized.
- **Savings are estimates.** EC2 prices come from the AWS Pricing API; storage prices are rough
  us-east-1 numbers. Savings Plans / RIs / discounts are not included.
- **Potential savings are not a bill drop.** The bill only falls after you apply the changes.
  Compare Cost Explorer month over month to get your real percentage.
- **Cost Explorer API costs about $0.01 per request.** One run uses a handful of requests.
- Reports contain resource IDs. Keep `reports/` out of git.

## Thresholds (env vars)
`LOOKBACK_DAYS=90`, `IDLE_CPU_P95=5`, `IDLE_CPU_PEAK=20`, `IDLE_NET_GB_DAY=0.5`,
`OVERSIZED_CPU_P95=30`, `OVERSIZED_CPU_PEAK=50`, `HOT_CPU_P95=80`, `OLD_SNAPSHOT_DAYS=90`.

## Run every Monday (cron)
    0 9 * * 1  cd /path/to/costpilot && .venv/bin/python costpilot.py 