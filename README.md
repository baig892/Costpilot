# CostPilot

Read-only AWS cost agent. Scans for waste, asks Claude to write a short report, posts to Slack.
It never changes or deletes anything.

## Run it
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    export AWS_REGION=us-east-1
    export ANTHROPIC_API_KEY=...        # optional
    export SLACK_WEBHOOK_URL=...        # optional
    python costpilot.py

## IAM
Attach `iam-policy.json` to the user/role that runs it. Read-only on purpose.

## Run every Monday (cron)
    0 9 * * 1  cd /path/to/costpilot && .venv/bin/python costpilot.py

## Track real savings
Each run saves `last_findings.json`. Keep a copy each week, then compare the AWS
bill (Cost Explorer) month over month. That is where your real % comes from.
