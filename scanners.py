"""
CostPilot v2 - scanners. Every AWS call in this file is READ-ONLY.

Main ideas:
- analyze_instances(): looks at 90 days of CPU + network per EC2 instance and
  gives a verdict (idle / oversized / ok / hot) with a recommended size.
- Other scanners: unattached volumes, gp2->gp3, old snapshots, unused Elastic
  IPs, idle RDS, idle NAT gateways, idle load balancers, log groups that never
  expire.
- cost_by_service(): real spend from Cost Explorer (last 30 days vs previous 30).
"""
import datetime as dt
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config


# ----------------------------- settings -------------------------------------
def _num(name, default):
    return float(os.getenv(name, default))


LOOKBACK_DAYS = int(_num("LOOKBACK_DAYS", 90))        # instance analysis window
SERVICE_LOOKBACK = int(_num("SERVICE_LOOKBACK", 30))  # RDS / NAT / ELB window
MIN_DAYS_FOR_VERDICT = int(_num("MIN_DAYS_FOR_VERDICT", 14))
IDLE_CPU_P95 = _num("IDLE_CPU_P95", 5)          # % CPU (95th percentile of hours)
IDLE_CPU_PEAK = _num("IDLE_CPU_PEAK", 20)       # % CPU (highest hour)
IDLE_NET_GB_DAY = _num("IDLE_NET_GB_DAY", 0.5)  # GB/day in+out
OVERSIZED_CPU_P95 = _num("OVERSIZED_CPU_P95", 30)
OVERSIZED_CPU_PEAK = _num("OVERSIZED_CPU_PEAK", 50)  # one size down doubles it
HOT_CPU_P95 = _num("HOT_CPU_P95", 80)
OLD_SNAPSHOT_DAYS = int(_num("OLD_SNAPSHOT_DAYS", 90))
HOURS_PER_MONTH = 730

# Rough us-east-1 prices (USD/month). ESTIMATES: confirm in Cost Explorer.
GP3_PER_GB, GP2_PER_GB, SNAPSHOT_PER_GB = 0.08, 0.10, 0.05
EIP_PER_MONTH, NAT_PER_MONTH, LB_PER_MONTH = 3.65, 32.85, 16.43
LOG_STORAGE_PER_GB = 0.03

CW_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})


# ----------------------------- pure helpers (unit-tested) -------------------
def percentile(values, pct):
    """Linear-interpolated percentile. Returns None for an empty list."""
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return v[int(k)]
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


_HALF = {
    "micro": "nano", "small": "micro", "medium": "small", "large": "medium",
    "xlarge": "large", "2xlarge": "xlarge", "4xlarge": "2xlarge",
    "8xlarge": "4xlarge", "12xlarge": "6xlarge", "16xlarge": "8xlarge",
    "24xlarge": "12xlarge", "32xlarge": "16xlarge", "48xlarge": "24xlarge",
}


def one_size_down(instance_type):
    """m5.2xlarge -> m5.xlarge. Returns None if there is no standard step down.
    The caller must still check the target really exists (describe_instance_types)."""
    if "." not in instance_type:
        return None
    family, size = instance_type.split(".", 1)
    smaller = _HALF.get(size)
    return f"{family}.{smaller}" if smaller else None


def summarize(cpu_avg, cpu_max, net_in, net_out):
    """Turn hourly metric points [(timestamp, value), ...] into simple stats."""
    if len(cpu_avg) < 48:  # less than 2 days of data
        return None
    vals = [v for _, v in cpu_avg]
    days = len(vals) / 24
    net_bytes = sum(v for _, v in net_in) + sum(v for _, v in net_out)
    peak = max((v for _, v in cpu_max), default=max(vals))
    return {
        "days_observed": round(days, 1),
        "cpu_avg": round(sum(vals) / len(vals), 1),
        "cpu_p95": round(percentile(vals, 95), 1),
        "cpu_peak": round(peak, 1),
        "net_gb_per_day": round(net_bytes / 1e9 / days, 2),
    }


def classify(stats):
    """idle / oversized / hot / ok / not_enough_data"""
    if not stats or stats["days_observed"] < MIN_DAYS_FOR_VERDICT:
        return "not_enough_data"
    if (stats["cpu_p95"] < IDLE_CPU_P95 and stats["cpu_peak"] < IDLE_CPU_PEAK
            and stats["net_gb_per_day"] < IDLE_NET_GB_DAY):
        return "idle"
    if stats["cpu_p95"] > HOT_CPU_P95:
        return "hot"
    if stats["cpu_p95"] < OVERSIZED_CPU_P95 and stats["cpu_peak"] < OVERSIZED_CPU_PEAK:
        return "oversized"
    return "ok"


def confidence(days_observed):
    if days_observed >= 60:
        return "high"
    if days_observed >= 30:
        return "medium"
    return "low"


def finding(ftype, rid, region, action, savings=None, name="", detail="", conf="medium"):
    return {
        "type": ftype, "id": rid, "region": region, "name": name,
        "detail": detail, "action": action,
        "est_monthly_savings": None if savings is None else round(savings, 2),
        "confidence": conf,
    }


# ----------------------------- CloudWatch helpers ---------------------------
def hourly_series(cw, namespace, metric, dimensions, stat, days):
    """Hourly datapoints [(timestamp, value)] for the last `days` days.
    GetMetricStatistics returns at most 1,440 points per call, so we fetch
    30-day chunks (720 hourly points each). CloudWatch keeps hourly data for
    455 days, so 90 days is fine."""
    end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(days=days)
    points, cur = [], start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=30), end)
        resp = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric, Dimensions=dimensions,
            StartTime=cur, EndTime=nxt, Period=3600, Statistics=[stat])
        points += [(p["Timestamp"], p[stat]) for p in resp["Datapoints"]]
        cur = nxt
    points.sort(key=lambda x: x[0])
    return points


def instance_stats(cw, instance_id, days):
    dims = [{"Name": "InstanceId", "Value": instance_id}]
    return summarize(
        hourly_series(cw, "AWS/EC2", "CPUUtilization", dims, "Average", days),
        hourly_series(cw, "AWS/EC2", "CPUUtilization", dims, "Maximum", days),
        hourly_series(cw, "AWS/EC2", "NetworkIn", dims, "Sum", days),
        hourly_series(cw, "AWS/EC2", "NetworkOut", dims, "Sum", days),
    )


# ----------------------------- pricing --------------------------------------
_price_cache = {}
_region_names = None


def _region_long_name(region):
    global _region_names
    if _region_names is None:
        from botocore.loaders import Loader
        data = Loader().load_data("endpoints")
        _region_names = {}
        for part in data["partitions"]:
            for code, info in part["regions"].items():
                _region_names[code] = info.get("description")
    return _region_names.get(region)


def hourly_price(instance_type, region, platform="Linux"):
    """On-demand hourly price from the AWS Pricing API. None if not found."""
    key = (instance_type, region, platform)
    if key in _price_cache:
        return _price_cache[key]
    price = None
    try:
        location = _region_long_name(region)
        if location:
            pricing = boto3.client("pricing", region_name="us-east-1")
            resp = pricing.get_products(
                ServiceCode="AmazonEC2", MaxResults=1,
                Filters=[{"Type": "TERM_MATCH", "Field": f, "Value": v} for f, v in [
                    ("instanceType", instance_type), ("location", location),
                    ("operatingSystem", platform), ("tenancy", "Shared"),
                    ("preInstalledSw", "NA"), ("capacitystatus", "Used")]])
            if resp["PriceList"]:
                item = json.loads(resp["PriceList"][0])
                term = next(iter(item["terms"]["OnDemand"].values()))
                dim = next(iter(term["priceDimensions"].values()))
                price = float(dim["pricePerUnit"]["USD"])
    except Exception:
        price = None
    _price_cache[key] = price
    return price


# ----------------------------- context --------------------------------------
class Ctx:
    def __init__(self, region):
        self.region = region
        self.ec2 = boto3.client("ec2", region_name=region)
        self.cw = boto3.client("cloudwatch", region_name=region, config=CW_CONFIG)
        self.rds = boto3.client("rds", region_name=region)
        self.elbv2 = boto3.client("elbv2", region_name=region)
        self.logs = boto3.client("logs", region_name=region)
        self._specs = {}

    def spec(self, instance_type):
        """vCPU and memory for an instance type. None if the type doesn't exist."""
        if instance_type not in self._specs:
            try:
                info = self.ec2.describe_instance_types(
                    InstanceTypes=[instance_type])["InstanceTypes"][0]
                self._specs[instance_type] = {
                    "vcpu": info["VCpuInfo"]["DefaultVCpus"],
                    "mem_gib": round(info["MemoryInfo"]["SizeInMiB"] / 1024, 1)}
            except Exception:
                self._specs[instance_type] = None
        return self._specs[instance_type]


def name_of(tags):
    for t in tags or []:
        if t["Key"] == "Name":
            return t["Value"]
    return ""


# ----------------------------- EC2 90-day analysis --------------------------
def _safe_stats(ctx, instance_id):
    try:
        return instance_stats(ctx.cw, instance_id, LOOKBACK_DAYS)
    except Exception:
        return None


def analyze_instances(ctx):
    """Returns (rows, findings). One row per running instance."""
    pages = ctx.ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
    instances = [i for p in pages for r in p["Reservations"] for i in r["Instances"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        all_stats = list(pool.map(lambda i: _safe_stats(ctx, i["InstanceId"]), instances))

    rows, findings = [], []
    for inst, stats in zip(instances, all_stats):
        itype, iid, nm = inst["InstanceType"], inst["InstanceId"], name_of(inst.get("Tags"))
        spec = ctx.spec(itype) or {}
        platform = "Windows" if inst.get("PlatformDetails", "").startswith("Windows") else "Linux"
        price = hourly_price(itype, ctx.region, platform)
        monthly = price * HOURS_PER_MONTH if price else None
        verdict = classify(stats)
        conf = confidence(stats["days_observed"]) if stats else "low"
        target, savings = None, None

        if verdict == "idle":
            rec = "Likely idle: stop or terminate (snapshot first if unsure)"
            savings = monthly
        elif verdict == "oversized":
            target = one_size_down(itype)
            if target and ctx.spec(target):
                tprice = hourly_price(target, ctx.region, platform)
                if monthly is not None:
                    new_monthly = tprice * HOURS_PER_MONTH if tprice else monthly / 2
                    savings = monthly - new_monthly
                rec = f"Downsize {itype} -> {target}"
            else:
                target = None
                rec = "Oversized, but no smaller size in this family. Try another family (e.g. Graviton)"
        elif verdict == "hot":
            rec = "Running hot: consider a bigger size or scaling out (performance risk)"
        elif verdict == "ok":
            rec = "Looks right-sized"
        else:
            rec = "Not enough data yet"

        notes = ["CPU and network only (memory is not checked)"]
        if itype.startswith("t"):
            notes.append("Burstable: also check CPUCreditBalance")
        row = {
            "region": ctx.region, "id": iid, "name": nm, "type": itype,
            "vcpu": spec.get("vcpu"), "mem_gib": spec.get("mem_gib"),
            "verdict": verdict, "recommendation": rec, "target_type": target,
            "est_monthly_savings": None if savings is None else round(savings, 2),
            "confidence": conf, "notes": "; ".join(notes), **(stats or {}),
        }
        rows.append(row)
        if verdict in ("idle", "oversized"):
            detail = (f"{stats['days_observed']} days: CPU avg {stats['cpu_avg']}%, "
                      f"p95 {stats['cpu_p95']}%, peak {stats['cpu_peak']}%, "
                      f"network {stats['net_gb_per_day']} GB/day")
            findings.append(finding(f"{verdict}_ec2", iid, ctx.region, rec, savings,
                                    name=nm, detail=detail, conf=conf))
    return rows, findings


# ----------------------------- storage / network scanners -------------------
def unattached_volumes(ctx):
    out = []
    for page in ctx.ec2.get_paginator("describe_volumes").paginate(
            Filters=[{"Name": "status", "Values": ["available"]}]):
        for v in page["Volumes"]:
            out.append(finding("unattached_ebs", v["VolumeId"], ctx.region,
                               "Snapshot if needed, then delete",
                               v["Size"] * GP3_PER_GB, name=name_of(v.get("Tags")),
                               detail=f"{v['Size']} GB not attached to anything", conf="high"))
    return out


def gp2_volumes(ctx):
    out = []
    for page in ctx.ec2.get_paginator("describe_volumes").paginate(
            Filters=[{"Name": "volume-type", "Values": ["gp2"]},
                     {"Name": "status", "Values": ["in-use"]}]):
        for v in page["Volumes"]:
            out.append(finding("gp2_to_gp3", v["VolumeId"], ctx.region,
                               "Change volume type to gp3 (online, no downtime)",
                               v["Size"] * (GP2_PER_GB - GP3_PER_GB),
                               name=name_of(v.get("Tags")), detail=f"{v['Size']} GB gp2",
                               conf="high"))
    return out


def old_snapshots(ctx):
    out, now = [], dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=OLD_SNAPSHOT_DAYS)
    for page in ctx.ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
        for s in page["Snapshots"]:
            if s["StartTime"] < cutoff:
                out.append(finding(
                    "old_snapshot", s["SnapshotId"], ctx.region,
                    "Check it is not used by an AMI or backup policy, then delete",
                    s["VolumeSize"] * SNAPSHOT_PER_GB,  # upper bound: snapshots are incremental
                    detail=f"{s['VolumeSize']} GB, {(now - s['StartTime']).days} days old",
                    conf="low"))
    return out


def unused_eips(ctx):
    out = []
    for a in ctx.ec2.describe_addresses()["Addresses"]:
        if "AssociationId" not in a:
            out.append(finding("unused_eip", a.get("AllocationId", a.get("PublicIp")),
                               ctx.region, "Release the Elastic IP", EIP_PER_MONTH,
                               detail=a.get("PublicIp", ""), conf="high"))
    return out


def idle_nat_gateways(ctx):
    out = []
    for page in ctx.ec2.get_paginator("describe_nat_gateways").paginate(
            Filter=[{"Name": "state", "Values": ["available"]}]):
        for n in page["NatGateways"]:
            pts = hourly_series(ctx.cw, "AWS/NATGateway", "BytesOutToDestination",
                                [{"Name": "NatGatewayId", "Value": n["NatGatewayId"]}],
                                "Sum", SERVICE_LOOKBACK)
            total_gb = sum(v for _, v in pts) / 1e9
            if total_gb < 1:
                out.append(finding(
                    "idle_nat", n["NatGatewayId"], ctx.region,
                    "Almost no traffic: check route tables, then delete",
                    NAT_PER_MONTH, name=name_of(n.get("Tags")),
                    detail=f"{total_gb:.2f} GB out in {SERVICE_LOOKBACK} days", conf="medium"))
    return out


def idle_load_balancers(ctx):
    out, now = [], dt.datetime.now(dt.timezone.utc)
    for page in ctx.elbv2.get_paginator("describe_load_balancers").paginate():
        for lb in page["LoadBalancers"]:
            if lb["State"]["Code"] != "active" or (now - lb["CreatedTime"]).days < 14:
                continue
            if lb["Type"] == "application":
                ns, metric = "AWS/ApplicationELB", "RequestCount"
            elif lb["Type"] == "network":
                ns, metric = "AWS/NetworkELB", "ProcessedBytes"
            else:
                continue
            suffix = lb["LoadBalancerArn"].split("loadbalancer/", 1)[1]
            pts = hourly_series(ctx.cw, ns, metric,
                                [{"Name": "LoadBalancer", "Value": suffix}],
                                "Sum", SERVICE_LOOKBACK)
            if sum(v for _, v in pts) == 0:  # no datapoints also means no traffic
                out.append(finding(
                    "idle_load_balancer", lb["LoadBalancerName"], ctx.region,
                    "No traffic: check what uses it, then delete",
                    LB_PER_MONTH, detail=f"{lb['Type']} LB, zero traffic in {SERVICE_LOOKBACK} days",
                    conf="medium"))
    return out


def idle_rds(ctx):
    out = []
    for page in ctx.rds.get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            if db["DBInstanceStatus"] != "available":
                continue
            dbid, cls = db["DBInstanceIdentifier"], db["DBInstanceClass"]
            dims = [{"Name": "DBInstanceIdentifier", "Value": dbid}]
            conns = hourly_series(ctx.cw, "AWS/RDS", "DatabaseConnections", dims,
                                  "Maximum", SERVICE_LOOKBACK)
            if conns and max(v for _, v in conns) == 0:
                out.append(finding(
                    "idle_rds", dbid, ctx.region,
                    "Zero connections: take a final snapshot, then delete "
                    "(stopping is temporary, AWS restarts it after 7 days)",
                    None, name=cls,
                    detail=f"no connections in {SERVICE_LOOKBACK} days", conf="medium"))
                continue
            cpu = [v for _, v in hourly_series(ctx.cw, "AWS/RDS", "CPUUtilization", dims,
                                               "Average", SERVICE_LOOKBACK)]
            p95 = percentile(cpu, 95)
            if p95 is not None and p95 < 10 and not cls.startswith("db.t"):
                out.append(finding(
                    "underused_rds", dbid, ctx.region,
                    "Low CPU: consider a smaller class (check memory and IOPS first)",
                    None, name=cls, detail=f"CPU p95 {p95:.1f}% over {SERVICE_LOOKBACK} days",
                    conf="low"))
    return out


def logs_without_retention(ctx):
    out = []
    for page in ctx.logs.get_paginator("describe_log_groups").paginate():
        for g in page["logGroups"]:
            gb = g.get("storedBytes", 0) / 1e9
            if "retentionInDays" not in g and gb > 1:
                out.append(finding(
                    "log_no_retention", g["logGroupName"], ctx.region,
                    "Set a retention period (for example 30-90 days)", None,
                    detail=f"{gb:.1f} GB stored, never expires (~${gb * LOG_STORAGE_PER_GB:.2f}/month)",
                    conf="high"))
    return out


SIMPLE_SCANNERS = [unattached_volumes, gp2_volumes, old_snapshots, unused_eips,
                   idle_nat_gateways, idle_load_balancers, idle_rds, logs_without_retention]


# ----------------------------- Cost Explorer --------------------------------
def cost_by_service(days=30):
    """Spend per service: last `days` days vs the `days` before.
    Note: each Cost Explorer API request costs about $0.01.
    UnblendedCost includes credits/refunds as negative amounts."""
    ce = boto3.client("ce", region_name="us-east-1")
    today = dt.date.today()

    def period(start, end):
        totals, token = {}, None
        while True:
            kwargs = dict(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="MONTHLY", Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])
            if token:
                kwargs["NextPageToken"] = token
            resp = ce.get_cost_and_usage(**kwargs)
            for r in resp["ResultsByTime"]:
                for g in r["Groups"]:
                    svc = g["Keys"][0]
                    totals[svc] = totals.get(svc, 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
            token = resp.get("NextPageToken")
            if not token:
                return totals

    cur = period(today - dt.timedelta(days=days), today)
    prev = period(today - dt.timedelta(days=2 * days), today - dt.timedelta(days=days))
    rows = []
    for svc in set(cur) | set(prev):
        c, p = cur.get(svc, 0.0), prev.get(svc, 0.0)
        rows.append({"service": svc, "last_30d": round(c, 2), "prev_30d": round(p, 2),
                     "change_pct": round((c - p) / p * 100, 1) if p > 0 else None})
    rows.sort(key=lambda r: r["last_30d"], reverse=True)
    return {"total_last_30d": round(sum(cur.values()), 2),
            "total_prev_30d": round(sum(prev.values()), 2), "services": rows}
