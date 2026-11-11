#!/usr/bin/env python3
"""
CostPilot v2 - read-only AWS cost agent.

What it does:
  1. Scans one or more regions (EC2 90-day usage, volumes, snapshots, IPs, RDS,
     NAT gateways, load balancers, log groups).
  2. Reads real spend from Cost Explorer (last 30 days vs previous 30).
  3. Writes a full report to ./reports (Markdown + CSV + JSON).
  4. Asks Claude for a short summary and posts it to Slack (or prints it).
It NEVER changes or deletes anything.

Env vars (all optional):
  AWS_REGION / REGIONS   e.g. "us-east-1" or "us-east-1,eu-west-1" or "all"
  ANTHROPIC_API_KEY      enables the AI summary
  CLAUDE_MODEL           default: claude-sonnet-5-5
  SLACK_WEBHOOK_URL      post the summary to Slack
  REPORT_DIR             default: reports
  LOOKBACK_DAYS          default: 90 (see scanners.py for all thresholds)
"""
import csv
import datetime as dt
import json
import os
from pathlib import Path

import boto3
import requests

import scanners as sc

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
OUT_DIR = Path(os.getenv("REPORT_DIR", "reports"))


# ----------------------------- scanning -------------------------------------
def get_regions():
    raw = os.getenv("REGIONS") or os.getenv("AWS_REGION", "us-east-1")
    if raw.strip().lower() == "all":
        ec2 = boto3.client("ec2", region_name=os.getenv("AWS_REGION", "us-east-1"))
        return [r["RegionName"] for r in ec2.describe_regions()["Regions"]]
    return [r.strip() for r in raw.split(",") if r.strip()]


def run_scan(regions):
    findings, instances, errors = [], [], []
    for region in regions:
        print(f"Scanning {region} ...")
        ctx = sc.Ctx(region)
        try:
            rows, f = sc.analyze_instances(ctx)
            instances += rows
            findings += f
        except Exception as e:
            errors.append(f"{region} / analyze_instances: {e}")
        for scanner in sc.SIMPLE_SCANNERS:
            try:
                findings += scanner(ctx)
            except Exception as e:
                errors.append(f"{region} / {scanner.__name__}: {e}")
    return findings, instances, errors


def total_savings(findings):
    return round(sum(f["est_monthly_savings"] or 0 for f in findings), 2)


def by_savings(findings):
    return sorted(findings, key=lambda f: f["est_monthly_savings"] or 0, reverse=True)


# ----------------------------- report files ---------------------------------
def money(x):
    return "n/a" if x is None else f"${x:,.2f}"


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in r) + " |")
    return "\n".join(out)


def build_markdown(today, regions, findings, instances, costs, errors):
    total = total_savings(findings)
    lines = [f"# CostPilot report - {today}", "",
             f"Regions: {', '.join(regions)}  ",
             f"Lookback: {sc.LOOKBACK_DAYS} days for EC2, {sc.SERVICE_LOOKBACK} days for other services", ""]

    lines += ["## Cost overview (last 30 days vs previous 30 days)", ""]
    if costs:
        c, p = costs["total_last_30d"], costs["total_prev_30d"]
        change = f"{(c - p) / p * 100:+.1f}%" if p > 0 else "n/a"
        lines += [f"Total: **{money(c)}** (previous 30 days: {money(p)}, change: {change})", ""]
        lines.append(md_table(
            ["Service", "Last 30d", "Previous 30d", "Change"],
            [[s["service"], money(s["last_30d"]), money(s["prev_30d"]),
              "n/a" if s["change_pct"] is None else f"{s['change_pct']:+.1f}%"]
             for s in costs["services"][:15]]))
    else:
        lines.append("Cost Explorer data was not available (see 'Checks that could not run').")

    lines += ["", "## Savings opportunities (estimates)", ""]
    share = ""
    if costs and costs["total_last_30d"] > 0:
        share = f" (about {total / costs['total_last_30d'] * 100:.1f}% of the last 30 days' spend)"
    lines += [f"Estimated potential savings: **{money(total)} per month**{share}.",
              "These are *potential* savings. Your bill only drops after you apply the changes.", ""]
    ranked = by_savings(findings)
    lines.append(md_table(
        ["Type", "Resource", "Region", "Details", "Action", "Est. $/mo", "Confidence"],
        [[f["type"], f"{f['id']} {f['name']}".strip(), f["region"], f["detail"],
          f["action"], money(f["est_monthly_savings"]), f["confidence"]] for f in ranked[:100]]))
    if len(ranked) > 100:
        lines.append(f"\n(Showing top 100 of {len(ranked)}. See the JSON file for all.)")

    lines += ["", f"## EC2 usage analysis ({sc.LOOKBACK_DAYS} days)", ""]
    if instances:
        order = {"idle": 0, "oversized": 1, "hot": 2, "ok": 3, "not_enough_data": 4}
        rows = sorted(instances, key=lambda r: (order[r["verdict"]], r["name"]))
        lines.append(md_table(
            ["Instance", "Name", "Type", "vCPU/Mem GiB", "Days", "CPU avg %", "CPU p95 %",
             "CPU peak %", "Net GB/day", "Verdict", "Recommendation"],
            [[r["id"], r["name"], r["type"], f"{r['vcpu']}/{r['mem_gib']}",
              r.get("days_observed", "-"), r.get("cpu_avg", "-"), r.get("cpu_p95", "-"),
              r.get("cpu_peak", "-"), r.get("net_gb_per_day", "-"), r["verdict"],
              r["recommendation"]] for r in rows]))
    else:
        lines.append("No running EC2 instances found.")

    lines += ["", "## Checks that could not run", ""]
    lines += [f"- {e}" for e in errors] if errors else ["None."]

    lines += ["", "## How to read this", "",
              "- Verdicts use CPU and network only. **Memory is not checked** (needs the CloudWatch agent). "
              "A low-CPU server can still be memory-heavy, so review before resizing.",
              "- p95 means 95% of hours were at or below that CPU level. Peak is the busiest hour.",
              "- Prices come from the AWS Pricing API when available, otherwise rough estimates. "
              "Confirm in Cost Explorer before acting.",
              "- Savings Plans, Reserved Instances and discounts are not included in the estimates."]
    return "\n".join(lines) + "\n"


def write_reports(today, md, findings, instances, costs):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = OUT_DIR / f"costpilot-{today}.md"
    md_path.write_text(md, encoding="utf-8")
    (OUT_DIR / f"findings-{today}.json").write_text(
        json.dumps({"findings": findings, "instances": instances, "costs": costs},
                   indent=2, default=str), encoding="utf-8")
    if instances:
        cols = sorted({k for r in instances for k in r})
        with open(OUT_DIR / f"instances-{today}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(instances)
    # One line per run, so you can track real progress month over month
    hist = OUT_DIR / "history.csv"
    new = not hist.exists()
    with open(hist, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["date", "spend_last_30d", "est_monthly_savings", "findings"])
        w.writerow([today, costs["total_last_30d"] if costs else "", total_savings(findings), len(findings)])
    return md_path


# ----------------------------- summary + Slack ------------------------------
def plain_summary(findings, costs):
    lines = ["*CostPilot report*"]
    if costs:
        lines.append(f"Spend, last 30 days: {money(costs['total_last_30d'])} "
                     f"(previous: {money(costs['total_prev_30d'])})")
    lines.append(f"Estimated potential savings: {money(total_savings(findings))}/month")
    for f in by_savings(findings)[:5]:
        lines.append(f"- {f['type']} {f['id']} ({f['region']}): {f['action']} "
                     f"[{money(f['est_monthly_savings'])}/mo, {f['confidence']} confidence]")
    return "\n".join(lines)


def ai_summary(findings, costs):
    """Short Slack message written by Claude. Falls back to plain text."""
    if not os.getenv("ANTHROPIC_API_KEY") or not findings:
        return plain_summary(findings, costs)
    try:
        from anthropic import Anthropic
        keep = ("type", "id", "region", "name", "detail", "action",
                "est_monthly_savings", "confidence")
        payload = {
            "total_estimated_monthly_savings": total_savings(findings),
            "cost_overview": costs and {
                "last_30d": costs["total_last_30d"], "previous_30d": costs["total_prev_30d"],
                "top_services": costs["services"][:8]},
            "top_findings": [{k: f[k] for k in keep} for f in by_savings(findings)[:40]],
        }
        prompt = (
            "You are a senior FinOps engineer. Below are findings from a read-only scan "
            "of an AWS account, as JSON. Write a short Slack message (max 220 words):\n"
            "1. One line: spend trend and total estimated monthly savings.\n"
            "2. Top 5 actions, biggest savings first, with resource ids and simple reasons.\n"
            "3. Split them into 'safe quick wins' and 'needs review first'.\n"
            "4. One caution line (memory is not measured, snapshots may back AMIs, "
            "estimates are not guaranteed).\n"
            "Use only numbers in the data. Do not invent anything. Simple English.\n\n"
            + json.dumps(payload, default=str))
        msg = Anthropic().messages.create(model=MODEL, max_tokens=900,
                                          messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if b.type == "text")
    except Exception as e:
        print(f"AI summary failed, using plain summary: {e}")
        return plain_summary(findings, costs)


def send_slack(text):
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        print("\n" + text)
        return
    requests.post(url, json={"text": text}, timeout=15).raise_for_status()


# ----------------------------- main -----------------------------------------
def main():
    today = dt.date.today().isoformat()
    regions = get_regions()
    findings, instances, errors = run_scan(regions)
    try:
        costs = sc.cost_by_service()
    except Exception as e:
        costs = None
        errors.append(f"cost_by_service (Cost Explorer): {e}")

    md = build_markdown(today, regions, findings, instances, costs, errors)
    path = write_reports(today, md, findings, instances, costs)
    print(f"\nFull report: {path}")
    send_slack(ai_summary(findings, costs))


if __name__ == "__main__":
    main()
    