"""
DynamoDB 封装 — 沙盒状态持久化、乐观锁(lease)、幂等键。

两张表:
  SANDBOXES_TABLE  : 主状态表,每沙盒一条记录
  EVENTS_TABLE     : 事件历史(旁路写,TTL 30 天自动清理)

并发安全:所有状态变更用条件写(ConditionalCheckFailedException → 409)。
注意:Table 对象每次调用时动态获取,确保 moto mock context 正确工作。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Attr

SANDBOXES_TABLE = os.environ.get("DYNAMODB_TABLE", "sandboxes")
EVENTS_TABLE    = os.environ.get("DYNAMODB_EVENTS_TABLE", "sandbox_events")
REGION          = os.environ.get("AWS_REGION", "us-east-1")
TAP_IDX_TABLE   = os.environ.get("DYNAMODB_TAPIDX_TABLE", "sandbox_tap_idx")


def _res():
    return boto3.resource("dynamodb", region_name=REGION)

def _sb():  return _res().Table(SANDBOXES_TABLE)
def _ev():  return _res().Table(EVENTS_TABLE)
def _tap(): return _res().Table(TAP_IDX_TABLE)


# ---------- 基础 CRUD ----------

def put(item: dict) -> None:
    _sb().put_item(Item=item, ConditionExpression=Attr("id").not_exists())


def get(sandbox_id: str) -> dict | None:
    resp = _sb().get_item(Key={"id": sandbox_id})
    item = resp.get("Item")
    return _from_dynamo(item) if item else None


def get_by_idempotency_key(key: str) -> dict | None:
    resp = _sb().query(
        IndexName="idempotency_key-index",
        KeyConditionExpression="idempotency_key = :k",
        ExpressionAttributeValues={":k": key},
        Limit=1,
    )
    items = resp.get("Items", [])
    return _from_dynamo(items[0]) if items else None


def list_by_tenant(tenant_id: str, limit: int = 100) -> list[dict]:
    resp = _sb().query(
        IndexName="tenant_id-updated_at-index",
        KeyConditionExpression="tenant_id = :t",
        ExpressionAttributeValues={":t": tenant_id},
        ScanIndexForward=False,
        Limit=limit,
    )
    return [_from_dynamo(i) for i in resp.get("Items", [])]


from decimal import Decimal


def _sanitize(v):
    """DynamoDB 不支持 float，转成字符串；其他类型原样返回。"""
    if isinstance(v, float):
        return str(v)
    return v


def _from_dynamo(obj):
    """DynamoDB 返回的 Decimal 转回 int/float，方便 JSON 序列化。"""
    if isinstance(obj, dict):
        return {k: _from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_dynamo(v) for v in obj]
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def update_state(
    sandbox_id: str,
    new_state: str,
    expected_state: str,
    extra: dict | None = None,
) -> None:
    now = _utcnow()
    update_expr = "SET #s = :new, updated_at = :now"
    attr_values: dict = {":new": new_state, ":expected": expected_state, ":now": now}
    attr_names: dict  = {"#s": "state"}

    if extra:
        for i, (k, v) in enumerate(extra.items()):
            placeholder = f":extra{i}"
            name_alias  = f"#extra{i}"
            update_expr += f", {name_alias} = {placeholder}"
            attr_names[name_alias]    = k        # 通过别名传字段名，防表达式注入
            attr_values[placeholder]  = _sanitize(v)

    _sb().update_item(
        Key={"id": sandbox_id},
        UpdateExpression=update_expr,
        ConditionExpression="#s = :expected",
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=attr_values,
    )


def force_update(sandbox_id: str, fields: dict) -> None:
    now  = _utcnow()
    sets = ", ".join(f"#{k} = :{k}" for k in fields)
    vals = {f":{k}": _sanitize(v) for k, v in fields.items()}
    vals[":now"] = now
    names = {f"#{k}": k for k in fields}
    _sb().update_item(
        Key={"id": sandbox_id},
        UpdateExpression=f"SET {sets}, updated_at = :now",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
    )


def delete(sandbox_id: str) -> None:
    _sb().delete_item(Key={"id": sandbox_id})


# ---------- Lease ----------

LEASE_DURATION_S = 60


def acquire_lease(sandbox_id: str, duration_s: int = LEASE_DURATION_S) -> str:
    lease_id = uuid.uuid4().hex
    expires  = _utcnow_plus(duration_s)
    now      = _utcnow()
    _sb().update_item(
        Key={"id": sandbox_id},
        UpdateExpression="SET lease_id = :lid, lease_expires = :exp",
        ConditionExpression="attribute_not_exists(lease_id) OR lease_expires < :now",
        ExpressionAttributeValues={":lid": lease_id, ":exp": expires, ":now": now},
    )
    return lease_id


def release_lease(sandbox_id: str, lease_id: str) -> None:
    try:
        _sb().update_item(
            Key={"id": sandbox_id},
            UpdateExpression="REMOVE lease_id, lease_expires",
            ConditionExpression="lease_id = :lid",
            ExpressionAttributeValues={":lid": lease_id},
        )
    except Exception:
        pass


# ---------- Warm Pool ----------

def mark_warm(sandbox_id: str) -> None:
    force_update(sandbox_id, {"pool_state": "warm", "state": "warm", "driver": os.environ.get("SANDBOX_DRIVER", "firecracker")})


def claim_warm_item(driver: str) -> str | None:
    resp = _sb().query(
        IndexName="pool_state-driver-index",
        KeyConditionExpression="pool_state = :w AND driver = :d",
        ExpressionAttributeValues={":w": "warm", ":d": driver},
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return None
    candidate = items[0]["id"]
    try:
        _sb().update_item(
            Key={"id": candidate},
            UpdateExpression="SET pool_state = :claimed",
            ConditionExpression="pool_state = :warm",
            ExpressionAttributeValues={":claimed": "claimed", ":warm": "warm"},
        )
        return candidate
    except Exception:
        return None


def count_warm(driver: str) -> int:
    resp = _sb().query(
        IndexName="pool_state-driver-index",
        KeyConditionExpression="pool_state = :w AND driver = :d",
        ExpressionAttributeValues={":w": "warm", ":d": driver},
        Select="COUNT",
    )
    return resp.get("Count", 0)


# ---------- tap_idx 分布式分配 ----------

def alloc_tap_idx() -> int:
    resp = _tap().update_item(
        Key={"node": "global"},
        UpdateExpression="ADD next_idx :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["next_idx"])


def release_tap_idx(idx: int) -> None:
    pass


# ---------- Events ----------

def write_event(sandbox_id: str, event: str, prev_state: str, detail: dict | None = None) -> None:
    safe_detail = {k: _sanitize(v) for k, v in (detail or {}).items()}
    _ev().put_item(Item={
        "id":         sandbox_id,
        "ts":         _utcnow(),
        "event":      event,
        "prev_state": prev_state,
        "detail":     safe_detail,
        "ttl":        int((_dt_now() + timedelta(days=30)).timestamp()),
    })


# ---------- 内部工具 ----------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow_plus(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _dt_now() -> datetime:
    return datetime.now(timezone.utc)
