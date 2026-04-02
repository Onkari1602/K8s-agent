"""
Persistent Store - DynamoDB backend for agent state
Stores: usage history, dedup tracker, scale-down counters, cost trends
Survives pod restarts. Serverless - no infrastructure to manage.
"""

import os
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

from . import config

logger = logging.getLogger("persistent-store")

TABLE_PREFIX = os.getenv("DYNAMO_TABLE_PREFIX", "k8s-healing-agent")
DYNAMO_REGION = os.getenv("DYNAMO_REGION", config.BEDROCK_REGION)
DYNAMO_ENDPOINT = os.getenv("DYNAMO_ENDPOINT_URL", "")
TTL_DAYS = int(os.getenv("DYNAMO_TTL_DAYS", "30"))


class PersistentStore:
    def __init__(self):
        kwargs = {"service_name": "dynamodb", "region_name": DYNAMO_REGION}
        if DYNAMO_ENDPOINT:
            kwargs["endpoint_url"] = DYNAMO_ENDPOINT
        self.dynamodb = boto3.resource(**kwargs)
        self.client = boto3.client(**kwargs)
        self.tables = {}
        self._ensure_tables()

    def _ensure_tables(self):
        """Create DynamoDB tables if they don't exist."""
        table_defs = {
            f"{TABLE_PREFIX}-state": {
                "KeySchema": [
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                "AttributeDefinitions": [
                    {"AttributeName": "pk", "AttributeType": "S"},
                    {"AttributeName": "sk", "AttributeType": "S"},
                ],
            },
            f"{TABLE_PREFIX}-usage": {
                "KeySchema": [
                    {"AttributeName": "deployment_key", "KeyType": "HASH"},
                    {"AttributeName": "timestamp", "KeyType": "RANGE"},
                ],
                "AttributeDefinitions": [
                    {"AttributeName": "deployment_key", "AttributeType": "S"},
                    {"AttributeName": "timestamp", "AttributeType": "S"},
                ],
            },
            f"{TABLE_PREFIX}-costs": {
                "KeySchema": [
                    {"AttributeName": "date", "KeyType": "HASH"},
                ],
                "AttributeDefinitions": [
                    {"AttributeName": "date", "AttributeType": "S"},
                ],
            },
        }

        existing = self.client.list_tables().get("TableNames", [])

        for table_name, schema in table_defs.items():
            if table_name in existing:
                self.tables[table_name] = self.dynamodb.Table(table_name)
                logger.debug(f"Table exists: {table_name}")
            else:
                try:
                    table = self.dynamodb.create_table(
                        TableName=table_name,
                        BillingMode="PAY_PER_REQUEST",
                        **schema,
                    )
                    table.wait_until_exists()
                    # Enable TTL
                    self.client.update_time_to_live(
                        TableName=table_name,
                        TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
                    )
                    self.tables[table_name] = table
                    logger.info(f"Created table: {table_name}")
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ResourceInUseException":
                        self.tables[table_name] = self.dynamodb.Table(table_name)
                    else:
                        logger.error(f"Failed to create table {table_name}: {e}")

    def _ttl(self, days=None):
        return int(time.time()) + ((days or TTL_DAYS) * 86400)

    def _decimal_convert(self, obj):
        """Convert floats to Decimal for DynamoDB."""
        if isinstance(obj, float):
            return Decimal(str(round(obj, 4)))
        if isinstance(obj, dict):
            return {k: self._decimal_convert(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._decimal_convert(i) for i in obj]
        return obj

    # =====================================================
    # Dedup Tracker
    # =====================================================
    def get_dedup(self, key: str) -> float:
        """Get last processed timestamp for a dedup key."""
        table = self.tables.get(f"{TABLE_PREFIX}-state")
        if not table:
            return 0
        try:
            resp = table.get_item(Key={"pk": "dedup", "sk": key})
            item = resp.get("Item")
            return float(item["timestamp"]) if item else 0
        except Exception:
            return 0

    def set_dedup(self, key: str, timestamp: float):
        """Set last processed timestamp for a dedup key."""
        table = self.tables.get(f"{TABLE_PREFIX}-state")
        if not table:
            return
        try:
            table.put_item(Item={
                "pk": "dedup",
                "sk": key,
                "timestamp": Decimal(str(timestamp)),
                "ttl": self._ttl(7),
            })
        except Exception as e:
            logger.error(f"Failed to set dedup: {e}")

    # =====================================================
    # Scale-Down / Right-Size Counters
    # =====================================================
    def get_counter(self, counter_type: str, key: str) -> int:
        """Get a counter value (scale_down, rightsize)."""
        table = self.tables.get(f"{TABLE_PREFIX}-state")
        if not table:
            return 0
        try:
            resp = table.get_item(Key={"pk": counter_type, "sk": key})
            item = resp.get("Item")
            return int(item["count"]) if item else 0
        except Exception:
            return 0

    def set_counter(self, counter_type: str, key: str, count: int):
        """Set a counter value."""
        table = self.tables.get(f"{TABLE_PREFIX}-state")
        if not table:
            return
        try:
            table.put_item(Item={
                "pk": counter_type,
                "sk": key,
                "count": count,
                "ttl": self._ttl(7),
            })
        except Exception as e:
            logger.error(f"Failed to set counter: {e}")

    def increment_counter(self, counter_type: str, key: str) -> int:
        """Increment and return new value."""
        current = self.get_counter(counter_type, key)
        new_val = current + 1
        self.set_counter(counter_type, key, new_val)
        return new_val

    def reset_counter(self, counter_type: str, key: str):
        """Reset counter to 0."""
        self.set_counter(counter_type, key, 0)

    # =====================================================
    # Usage History
    # =====================================================
    def store_usage(self, deployment_key: str, snapshot: dict):
        """Store a usage snapshot for a deployment."""
        table = self.tables.get(f"{TABLE_PREFIX}-usage")
        if not table:
            return
        try:
            ts = datetime.now(timezone.utc).isoformat()
            item = self._decimal_convert({
                "deployment_key": deployment_key,
                "timestamp": ts,
                "cpu_usage_m": snapshot.get("cpu_usage_m", 0),
                "memory_usage_mi": snapshot.get("memory_usage_mi", 0),
                "cpu_request_m": snapshot.get("cpu_request_m", 0),
                "memory_request_mi": snapshot.get("memory_request_mi", 0),
                "ttl": self._ttl(7),
            })
            table.put_item(Item=item)
        except Exception as e:
            logger.error(f"Failed to store usage: {e}")

    def get_usage_history(self, deployment_key: str, limit: int = 30) -> list:
        """Get recent usage snapshots for a deployment."""
        table = self.tables.get(f"{TABLE_PREFIX}-usage")
        if not table:
            return []
        try:
            resp = table.query(
                KeyConditionExpression="deployment_key = :dk",
                ExpressionAttributeValues={":dk": deployment_key},
                ScanIndexForward=False,
                Limit=limit,
            )
            items = resp.get("Items", [])
            return [
                {
                    "timestamp": item["timestamp"],
                    "cpu_usage_m": int(item.get("cpu_usage_m", 0)),
                    "memory_usage_mi": int(item.get("memory_usage_mi", 0)),
                    "cpu_request_m": int(item.get("cpu_request_m", 0)),
                    "memory_request_mi": int(item.get("memory_request_mi", 0)),
                }
                for item in reversed(items)
            ]
        except Exception as e:
            logger.error(f"Failed to get usage history: {e}")
            return []

    # =====================================================
    # Cost Trends
    # =====================================================
    def store_daily_cost(self, date: str, data: dict):
        """Store daily cost snapshot."""
        table = self.tables.get(f"{TABLE_PREFIX}-costs")
        if not table:
            return
        try:
            item = self._decimal_convert({
                "date": date,
                "cluster_cost": data.get("cluster_monthly_cost", 0),
                "namespaces": json.dumps(data.get("namespaces", {})),
                "ttl": self._ttl(90),
            })
            table.put_item(Item=item)
        except Exception as e:
            logger.error(f"Failed to store daily cost: {e}")

    def get_cost_trend(self, days: int = 30) -> list:
        """Get cost trend for last N days."""
        table = self.tables.get(f"{TABLE_PREFIX}-costs")
        if not table:
            return []
        try:
            from datetime import timedelta
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=days)
            results = []

            resp = table.scan(
                FilterExpression="attribute_exists(#d) AND #d >= :start",
                ExpressionAttributeNames={"#d": "date"},
                ExpressionAttributeValues={":start": start.strftime("%Y-%m-%d")},
            )

            for item in sorted(resp.get("Items", []), key=lambda x: x["date"]):
                results.append({
                    "date": item["date"],
                    "cluster_monthly_cost": float(item.get("cluster_cost", 0)),
                    "namespaces": json.loads(item.get("namespaces", "{}")),
                })
            return results
        except Exception as e:
            logger.error(f"Failed to get cost trend: {e}")
            return []

    # =====================================================
    # Agent Actions Log
    # =====================================================
    def log_action(self, action_type: str, target: str, details: str, success: bool):
        """Log an agent action for audit trail."""
        table = self.tables.get(f"{TABLE_PREFIX}-state")
        if not table:
            return
        try:
            ts = datetime.now(timezone.utc).isoformat()
            table.put_item(Item={
                "pk": "action_log",
                "sk": ts,
                "action_type": action_type,
                "target": target,
                "details": details,
                "success": success,
                "ttl": self._ttl(30),
            })
        except Exception as e:
            logger.error(f"Failed to log action: {e}")

    def get_action_log(self, limit: int = 50) -> list:
        """Get recent agent actions."""
        table = self.tables.get(f"{TABLE_PREFIX}-state")
        if not table:
            return []
        try:
            resp = table.query(
                KeyConditionExpression="pk = :pk",
                ExpressionAttributeValues={":pk": "action_log"},
                ScanIndexForward=False,
                Limit=limit,
            )
            return [
                {
                    "timestamp": item["sk"],
                    "type": item.get("action_type", ""),
                    "target": item.get("target", ""),
                    "details": item.get("details", ""),
                    "success": item.get("success", True),
                }
                for item in resp.get("Items", [])
            ]
        except Exception as e:
            logger.error(f"Failed to get action log: {e}")
            return []
