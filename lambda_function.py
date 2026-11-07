import os
from datetime import datetime, timedelta, timezone

import boto3

SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
IDLE_BYTES = int(os.environ.get("IDLE_BYTES_THRESHOLD", "1000000"))  # under 1 MB = idle

# Approximate us-east-1 list prices. Verify on the AWS pricing pages.
HOURS_PER_MONTH = 730
EIP_PER_HOUR = 0.005
NAT_PER_HOUR = 0.045
EBS_PER_GB_MONTH = {"gp3": 0.08, "gp2": 0.10}
EBS_DEFAULT_PER_GB_MONTH = 0.10

ec2 = boto3.client("ec2")
cloudwatch = boto3.client("cloudwatch")
sns = boto3.client("sns")


def find_unattached_eips():
    findings = []
    for addr in ec2.describe_addresses()["Addresses"]:
        if "AssociationId" not in addr:
            findings.append({
                "type": "Unattached Elastic IP",
                "id": addr.get("AllocationId", addr["PublicIp"]),
                "detail": addr["PublicIp"],
                "monthly_cost": round(EIP_PER_HOUR * HOURS_PER_MONTH, 2),
            })
    return findings


def find_unattached_volumes():
    findings = []
    paginator = ec2.get_paginator("describe_volumes")
    pages = paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}])
    for page in pages:
        for vol in page["Volumes"]:
            price = EBS_PER_GB_MONTH.get(vol["VolumeType"], EBS_DEFAULT_PER_GB_MONTH)
            findings.append({
                "type": "Unattached EBS volume",
                "id": vol["VolumeId"],
                "detail": f"{vol['Size']} GB {vol['VolumeType']}",
                "monthly_cost": round(vol["Size"] * price, 2),
            })
    return findings


def find_idle_nat_gateways():
    findings = []
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    paginator = ec2.get_paginator("describe_nat_gateways")
    pages = paginator.paginate(Filters=[{"Name": "state", "Values": ["available"]}])
    for page in pages:
        for nat in page["NatGateways"]:
            stats = cloudwatch.get_metric_statistics(
                Namespace="AWS/NATGateway",
                MetricName="BytesOutToDestination",
                Dimensions=[{"Name": "NatGatewayId", "Value": nat["NatGatewayId"]}],
                StartTime=start,
                EndTime=end,
                Period=3600,
                Statistics=["Sum"],
            )
            total_bytes = sum(dp["Sum"] for dp in stats["Datapoints"])
            if total_bytes < IDLE_BYTES:
                findings.append({
                    "type": "Idle NAT gateway",
                    "id": nat["NatGatewayId"],
                    "detail": f"{int(total_bytes)} bytes out in {LOOKBACK_DAYS} days",
                    "monthly_cost": round(NAT_PER_HOUR * HOURS_PER_MONTH, 2),
                })
    return findings


def lambda_handler(event, context):
    findings = (
        find_unattached_eips()
        + find_unattached_volumes()
        + find_idle_nat_gateways()
    )
    monthly = round(sum(f["monthly_cost"] for f in findings), 2)
    annual = round(monthly * 12, 2)

    lines = ["AWS NETWORK WASTE AUDIT", "=" * 40, ""]
    if not findings:
        lines.append("No waste found. Nice work.")
    for f in findings:
        lines.append(f"- {f['type']}: {f['id']} ({f['detail']}) ~${f['monthly_cost']}/month")
    lines += [
        "",
        f"Estimated avoidable cost: ${monthly}/month (${annual}/year)",
        "Notes: NAT figures exclude data-processing charges. Prices are estimates.",
        "This report is read-only. Nothing was deleted.",
    ]
    message = "\n".join(lines)

    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=f"AWS waste audit: ${monthly}/month avoidable",
        Message=message,
    )
    print(message)
    return {"findings": len(findings), "monthly": monthly, "annual": annual}
