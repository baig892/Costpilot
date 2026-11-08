#!/usr/bin/env python3
"""
CostPilot - a read-only AWS cost recommendation agent.

It scans your account, finds waste, asks Claude to write a short prioritized
report, and posts it to Slack. It NEVER changes or deletes anything.

Env vars:
  AWS_REGION           default: us-east-1
  ANTHROPIC_API_KEY    optional. Without it you get a plain-text report.
  CLAUDE_MODEL         optional. default: claude-sonnet-5-5
  SLACK_WEBHOOK_URL    optional. Without it the report prints to the console.
  IDLE_DAYS            default: 14   (how far back to check CPU)
  IDLE_CPU_PERCENT     default: 5    (avg CPU below this = idle)
  OLD_SNAPSHOT_DAYS    default: 90
"""
import os
import json
import datetime as dt

import boto3
import requests

REGION = os.getenv("AWS_REGION", "us-east-1")
IDLE_DAYS = int(os.getenv("IDLE_DAYS", "14"))
IDLE_CPU = float(os.getenv("IDLE_CPU_PERCENT", "5"))
OLD_SNAPSHOT_DAYS = int(os.getenv("OLD_SNAPSHOT_DAYS", "90"))
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

# Rough us-east-1 prices (USD/month). These are ESTIMATES. Always confirm in
# Cost Explorer before acting.
GP3_PER_GB = 0.08
GP2_PER_GB = 0.10
SNAPSHOT_PER_GB = 0.05
EIP_PER_MONTH = 3.65
EC2_MONTHLY = {  # on-demand Linux, approximate
    "t3.micro": 7.6, "t3.small": 15.2, "t3.medium": 30.4, "t3.large": 60.8,
    "t3.xlarge": 121.6, "m5.large": 70.1, "m5.xlarge": 140.2,
    "m5.2xlarge": 280.3, "c5.large": 62.1, "c5.xlarge": 124.1,
    "r5.large": 91.3, "r5.xlarge": 182.5,
}

ec2 = boto3.client("ec2", region_name=REGION)
cw = boto3.client("cloudwatch", region_name=REGION)


def name_of(tags):
    for t in tags or []:
        if t["Key"] == "Name":
            return t["Value"]
    return "(no name)"


def find_idle_instances():
    findings = []
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=IDLE_DAYS)
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    )
    for page in pages:
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                stats = cw.get_metric_statistics(
                    Namespace="AWS/EC2",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "InstanceId", "Value": inst["InstanceId"]}],
                    StartTime=start,
                    EndTime=end,
                    Period=86400,
                    Statistics=["Average"],
                )
                points = [p["Average"] for p in stats["Datapoints"]]
                if len(points) < max(3, IDLE_DAYS // 2):
                    continue  # not enough data to judge
                avg = sum(points) / len(points)
                if avg < IDLE_CPU:
                    itype = inst["InstanceType"]
                    findings.append({
                        "type": "idle_ec2",
                        "id": inst["InstanceId"],
                        "name": name_of(inst.get("Tags")),
                        "instance_type": itype,
                        "avg_cpu_percent": round(avg, 2),
                        "days_checked": len(points),
                        "est_monthly_savings": EC2_MONTHLY.get(itype),
                        "action": "Stop or downsize after checking memory/network use",
                    })
    return findings


def find_unattached_volumes():
    findings = []
    pages = ec2.get_paginator("describe_volumes").paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    )
    for page in pages:
        for v in page["Volumes"]:
            findings.append({
                "type": "unattached_ebs",
                "id": v["VolumeId"],
                "name": name_of(v.get("Tags")),
                "size_gb": v["Size"],
                "est_monthly_savings": round(v["Size"] * GP3_PER_GB, 2),
                "action": "Snapshot if needed, then delete",
            })
    return findings


def find_gp2_volumes():
    findings = []
    pages = ec2.get_paginator("describe_volumes").paginate(
        Filters=[{"Name": "volume-type", "Values": ["gp2"]},
                 {"Name": "status", "Values": ["in-use"]}]
    )
    for page in pages:
        for v in page["Volumes"]:
            findings.append({
                "type": "gp2_to_gp3",
                "id": v["VolumeId"],
                "name": name_of(v.get("Tags")),
                "size_gb": v["Size"],
                "est_monthly_savings": round(v["Size"] * (GP2_PER_GB - GP3_PER_GB), 2),
                "action": "Modify volume type to gp3 (online, no downtime)",
            })
    return findings


def find_old_snapshots():
    findings = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=OLD_SNAPSHOT_DAYS)
    pages = ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"])
    for page in pages:
        for s in page["Snapshots"]:
            if s["StartTime"] < cutoff:
                findings.append({
                    "type": "old_snapshot",
                    "id": s["SnapshotId"],
                    "size_gb": s["VolumeSize"],
                    "age_days": (dt.datetime.now(dt.timezone.utc) - s["StartTime"]).days,
                    # Snapshots are incremental, so real savings are often lower.
                    "est_monthly_savings": round(s["VolumeSize"] * SNAPSHOT_PER_GB, 2),
                    "action": "Check it is not used by an AMI or backup policy, then delete",
                })
    return findings


def find_unused_eips():
    findings = []
    for a in ec2.describe_addresses()["Addresses"]:
        if "AssociationId" not in a:
            findings.append({
                "type": "unused_eip",
                "id": a.get("AllocationId", a.get("PublicIp")),
                "ip": a.get("PublicIp"),
                "est_monthly_savings": EIP_PER_MONTH,
                "action": "Release the Elastic IP",
            })
    return findings


def total_savings(findings):
    return round(sum(f.get("est_monthly_savings") or 0 for f in findings), 2)


def plain_report(findings):
    lines = [f"CostPilot report ({REGION})",
             f"Estimated monthly savings: ${total_savings(findings)}", ""]
    if not findings:
        lines.append("No waste found. Nice.")
    for f in sorted(findings, key=lambda x: x.get("est_monthly_savings") or 0, reverse=True):
        save = f.get("est_monthly_savings")
        save_txt = f"~${save}/mo" if save is not None else "savings: check Cost Explorer"
        lines.append(f"- [{f['type']}] {f['id']} {f.get('name', '')} | {save_txt} | {f['action']}")
    return "\n".join(lines)


def ai_report(findings):
    """Ask Claude to write a short, prioritized report. Falls back to plain text."""
    if not os.getenv("ANTHROPIC_API_KEY") or not findings:
        return plain_report(findings)
    try:
        from anthropic import Anthropic
        client = Anthropic()
        prompt = (
            "You are a senior FinOps engineer. Below are cost findings from an AWS "
            f"account ({REGION}) as JSON. Total estimated monthly savings: "
            f"${total_savings(findings)}.\n\n"
            "Write a short Slack message:\n"
            "1. One line with the total savings.\n"
            "2. Top 5 actions, biggest savings first, each with the resource id and "
            "the reason in plain simple English.\n"
            "3. One 'be careful' line for anything risky (e.g. snapshots used by AMIs, "
            "instances that may be low CPU but high memory).\n"
            "Savings are estimates. Do not invent numbers. Keep it under 200 words.\n\n"
            f"{json.dumps(findings, default=str)[:60000]}"
        )
        msg = client.messages.create(
            model=MODEL, max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if b.type == "text")
    except Exception as e:  # never let the AI layer break the report
        print(f"AI summary failed, using plain report: {e}")
        return plain_report(findings)


def send_slack(text):
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        print(text)
        return
    r = requests.post(url, json={"text": text}, timeout=15)
    r.raise_for_status()


def main():
    findings = []
    for scanner in (find_idle_instances, find_unattached_volumes,
                    find_gp2_volumes, find_old_snapshots, find_unused_eips):
        try:
            findings += scanner()
        except Exception as e:
            print(f"{scanner.__name__} failed: {e}")
    # Keep the raw data so you can track real savings month over month
    with open("last_findings.json", "w") as fh:
        json.dump(findings, fh, indent=2, default=str)
    send_slack(ai_report(findings))


if __name__ == "__main__":
    main()