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
from botocore.exceptions import ClientError

SANDBOXES_TABLE = os.environ.get("DYNAMODB_TABLE", "sandboxes")
EVENTS_TABLE    = os.environ.get("DYNAMODB_EVENTS_TABLE", "sandbox_events")
REGION          = os.environ.get("AWS_REGION", "us-east-1")
TAP_IDX_TABLE   = os.environ.get("DYNAMODB_TAPIDX_TABLE", "sandbox_tap_idx")
NODES_TABLE     = os.environ.get("DYNAMODB_NODES_TABLE", "sandbox_nodes")
LOCKS_TABLE     = os.environ.get("DYNAMODB_LOCKS_TABLE", "sandbox_locks")


def _res():
    return boto3.resource("dynamodb", region_name=REGION)

def _sb():    return _res().Table(SANDBOXES_TABLE)
def _ev():    return _res().Table(EVENTS_TABLE)
def _tap():   return _res().Table(TAP_IDX_TABLE)
def _nodes(): return _res().Table(NODES_TABLE)
def _locks(): return _res().Table(LOCKS_TABLE)


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


def list_by_states(states: list[str]) -> list[dict]:
    """
    通过 state-updated_at-index 查询 lifecycle 状态。

    老环境在 GSI 在线创建完成前会返回 ValidationException，此时暂时回退旧的
    scan 路径，支持无停机滚动升级；新环境和索引就绪后不再做全表扫描。
    """
    if not states:
        return []
    try:
        items: list[dict] = []
        for state in states:
            kwargs: dict = {
                "IndexName": "state-updated_at-index",
                "KeyConditionExpression": "#s = :state",
                "ExpressionAttributeNames": {"#s": "state"},
                "ExpressionAttributeValues": {":state": state},
            }
            while True:
                resp = _sb().query(**kwargs)
                items.extend(resp.get("Items", []))
                lek = resp.get("LastEvaluatedKey")
                if not lek:
                    break
                kwargs["ExclusiveStartKey"] = lek
        return [_from_dynamo(i) for i in items]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "ValidationException",
            "ResourceNotFoundException",
        }:
            raise

    # Rolling-upgrade fallback while the new GSI is not ACTIVE yet.
    # FilterExpression: #s IN (:s0, :s1, ...)
    placeholders = {f":s{i}": s for i, s in enumerate(states)}
    filter_expr  = "#s IN (" + ", ".join(placeholders) + ")"
    items: list[dict] = []
    kwargs: dict = {
        "FilterExpression": filter_expr,
        "ExpressionAttributeNames": {"#s": "state"},
        "ExpressionAttributeValues": placeholders,
    }
    while True:
        resp = _sb().scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return [_from_dynamo(i) for i in items]


def list_by_recovery_target_instance(
    instance_id: str,
    *,
    consistent: bool = False,
) -> list[dict]:
    """Return every durable sandbox still owned by one recovery host.

    The GSI path is used for cheap polling. Destructive EBS handoff calls pass
    ``consistent=True`` and scan the base table so an eventually-consistent
    index can never hide a suspended sandbox from the OD recycle fence.
    """
    if not instance_id:
        return []
    if consistent:
        return _scan_by_recovery_target_instance(instance_id)

    items: list[dict] = []
    query_kwargs: dict = {
        "IndexName": (
            "recovery_target_instance_id-updated_at-index"
        ),
        "KeyConditionExpression": "#target = :target",
        "ExpressionAttributeNames": {
            "#target": "recovery_target_instance_id",
        },
        "ExpressionAttributeValues": {
            ":target": instance_id,
        },
    }
    try:
        while True:
            response = _sb().query(**query_kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        if items:
            return [_from_dynamo(item) for item in items]
        # A GSI is eventually consistent. Confirm an apparently empty host
        # with the strongly consistent base table before reclaiming its EBS.
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "ValidationException",
            "ResourceNotFoundException",
        }:
            raise

    # Rolling-upgrade fallback while the GSI is absent/building, and the
    # strong empty-result confirmation described above.
    return _scan_by_recovery_target_instance(instance_id)


def _scan_by_recovery_target_instance(instance_id: str) -> list[dict]:
    """Strongly consistent ownership fence used before destructive recycle."""
    items: list[dict] = []
    kwargs: dict = {
        "ConsistentRead": True,
        "FilterExpression": "#target = :target",
        "ExpressionAttributeNames": {
            "#target": "recovery_target_instance_id",
        },
        "ExpressionAttributeValues": {
            ":target": instance_id,
        },
    }
    while True:
        response = _sb().scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return [_from_dynamo(item) for item in items]


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
        # Never let a late duplicate reconciler recreate a key-only item
        # after the delete finalizer removed the sandbox projection.
        ConditionExpression=(
            "attribute_exists(id) AND "
            "(attribute_not_exists(lease_id) OR lease_expires < :now)"
        ),
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


def renew_lease(
    sandbox_id: str,
    lease_id: str,
    duration_s: int = LEASE_DURATION_S,
) -> bool:
    """Extend a per-sandbox operation lease only while this owner still holds it."""
    try:
        _sb().update_item(
            Key={"id": sandbox_id},
            UpdateExpression="SET lease_expires = :exp",
            ConditionExpression="lease_id = :lid",
            ExpressionAttributeValues={
                ":lid": lease_id,
                ":exp": _utcnow_plus(duration_s),
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get(
            "Code"
        ) == "ConditionalCheckFailedException":
            return False
        raise


# ---------- 节点心跳注册表(P0-3) ----------

def heartbeat_node(node_id: str, ip: str, free_mem_mib: int,
                   vm_count: int, labels: dict | None = None) -> None:
    """node-agent 定期 upsert 一条心跳。last_seen 由控制面按 TTL 判活。"""
    _nodes().put_item(Item={
        "node_id":      node_id,
        "ip":           ip,
        "free_mem_mib": int(free_mem_mib),
        "vm_count":     int(vm_count),
        "last_seen":    _utcnow(),
        "labels":       labels or {},
    })


def list_active_nodes(ttl_s: int = 90) -> list[dict]:
    """
    返回 last_seen 在 ttl_s 内的活节点。scan 全表(节点数量级小,可接受)。
    死节点(超时)不返回 → 自动从调度池剔除。
    """
    cutoff = _utcnow_minus(ttl_s)
    resp   = _nodes().scan()
    out: list[dict] = []
    for item in resp.get("Items", []):
        if item.get("last_seen", "") >= cutoff:
            out.append(_from_dynamo(item))
    return out


def claim_recovery_standby(
    availability_zone: str,
    recovery_session_id: str,
    claim_ttl_s: int = 1800,
) -> dict | None:
    """Atomically claim one live, empty standby in the requested AZ."""
    now = _utcnow()
    expires = _utcnow_plus(claim_ttl_s)
    candidates = sorted(
        list_active_nodes(),
        key=lambda item: (
            -int(item.get("free_mem_mib", 0)),
            item.get("node_id", ""),
        ),
    )
    for item in candidates:
        if item.get("availability_zone") != availability_zone:
            continue
        if item.get("recovery_role") != "standby":
            continue
        if item.get("state_volume_id"):
            continue
        if int(item.get("vm_count", 0)) != 0:
            continue
        if bool(item.get("draining")):
            continue
        node_id = item.get("node_id")
        if not node_id:
            continue
        try:
            response = _nodes().update_item(
                Key={"node_id": node_id},
                UpdateExpression=(
                    "SET recovery_claim_id=:session, "
                    "recovery_claim_expires=:expires"
                ),
                ConditionExpression=(
                    "#role=:standby AND #vms=:zero AND "
                    "(attribute_not_exists(#draining) OR "
                    "#draining=:false) AND "
                    "(attribute_not_exists(#volume) OR #volume=:empty) AND "
                    "(attribute_not_exists(recovery_claim_id) OR "
                    "recovery_claim_expires < :now OR "
                    "recovery_claim_id = :session)"
                ),
                ExpressionAttributeNames={
                    "#role": "recovery_role",
                    "#vms": "vm_count",
                    "#draining": "draining",
                    "#volume": "state_volume_id",
                },
                ExpressionAttributeValues={
                    ":standby": "standby",
                    ":zero": 0,
                    ":false": False,
                    ":empty": "",
                    ":session": recovery_session_id,
                    ":expires": expires,
                    ":now": now,
                },
                ReturnValues="ALL_NEW",
            )
            return _from_dynamo(response.get("Attributes") or item)
        except ClientError as exc:
            if exc.response.get("Error", {}).get(
                "Code"
            ) == "ConditionalCheckFailedException":
                continue
            raise
    return None


def get_recovery_claim(recovery_session_id: str) -> dict | None:
    for item in list_active_nodes():
        if item.get("recovery_claim_id") == recovery_session_id:
            return item
    return None


def release_recovery_claim(
    node_id: str,
    recovery_session_id: str,
) -> None:
    try:
        _nodes().update_item(
            Key={"node_id": node_id},
            UpdateExpression=(
                "REMOVE recovery_claim_id, recovery_claim_expires"
            ),
            ConditionExpression="recovery_claim_id = :session",
            ExpressionAttributeValues={":session": recovery_session_id},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get(
            "Code"
        ) != "ConditionalCheckFailedException":
            raise


def claim_repatriation_target(
    availability_zone: str,
    recovery_group: str,
    recovery_session_id: str,
    claim_ttl_s: int = 1800,
) -> dict | None:
    """Atomically reserve one empty active Spot node for an EBS handoff."""
    now = _utcnow()
    expires = _utcnow_plus(claim_ttl_s)
    candidates = sorted(
        list_active_nodes(),
        key=lambda item: (
            -int(item.get("free_mem_mib", 0)),
            item.get("node_id", ""),
        ),
    )
    for item in candidates:
        if item.get("availability_zone") != availability_zone:
            continue
        if item.get("recovery_group") != recovery_group:
            continue
        if item.get("recovery_role") != "active":
            continue
        if item.get("pool") != "spot":
            continue
        if not item.get("state_volume_id"):
            continue
        if int(item.get("vm_count", 0)) != 0:
            continue
        if bool(item.get("draining")):
            continue
        if item.get("recovery_claim_id"):
            continue
        node_id = item.get("node_id")
        if not node_id:
            continue
        try:
            response = _nodes().update_item(
                Key={"node_id": node_id},
                UpdateExpression=(
                    "SET repatriation_claim_id=:session, "
                    "repatriation_claim_expires=:expires, "
                    "repatriation_replaced_volume_id=#volume"
                ),
                ConditionExpression=(
                    "#role=:active AND #pool=:spot AND #vms=:zero AND "
                    "#az=:az AND #group=:group AND "
                    "attribute_exists(#volume) AND #volume <> :empty AND "
                    "(attribute_not_exists(#draining) OR "
                    "#draining=:false) AND "
                    "attribute_not_exists(recovery_claim_id) AND "
                    "(attribute_not_exists(repatriation_claim_id) OR "
                    "repatriation_claim_expires < :now OR "
                    "repatriation_claim_id = :session)"
                ),
                ExpressionAttributeNames={
                    "#role": "recovery_role",
                    "#pool": "pool",
                    "#vms": "vm_count",
                    "#az": "availability_zone",
                    "#group": "recovery_group",
                    "#volume": "state_volume_id",
                    "#draining": "draining",
                },
                ExpressionAttributeValues={
                    ":active": "active",
                    ":spot": "spot",
                    ":zero": 0,
                    ":az": availability_zone,
                    ":group": recovery_group,
                    ":empty": "",
                    ":false": False,
                    ":session": recovery_session_id,
                    ":expires": expires,
                    ":now": now,
                },
                ReturnValues="ALL_NEW",
            )
            return _from_dynamo(response.get("Attributes") or item)
        except ClientError as exc:
            if exc.response.get("Error", {}).get(
                "Code"
            ) == "ConditionalCheckFailedException":
                continue
            raise
    return None


def get_repatriation_claim(recovery_session_id: str) -> dict | None:
    for item in list_active_nodes():
        if item.get("repatriation_claim_id") == recovery_session_id:
            return item
    return None


def renew_repatriation_claim(
    node_id: str,
    recovery_session_id: str,
    claim_ttl_s: int = 1800,
) -> bool:
    try:
        _nodes().update_item(
            Key={"node_id": node_id},
            UpdateExpression="SET repatriation_claim_expires=:expires",
            ConditionExpression="repatriation_claim_id = :session",
            ExpressionAttributeValues={
                ":session": recovery_session_id,
                ":expires": _utcnow_plus(claim_ttl_s),
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get(
            "Code"
        ) == "ConditionalCheckFailedException":
            return False
        raise


def release_repatriation_claim(
    node_id: str,
    recovery_session_id: str,
) -> None:
    try:
        _nodes().update_item(
            Key={"node_id": node_id},
            UpdateExpression=(
                "REMOVE repatriation_claim_id, repatriation_claim_expires, "
                "repatriation_replaced_volume_id"
            ),
            ConditionExpression="repatriation_claim_id = :session",
            ExpressionAttributeValues={":session": recovery_session_id},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get(
            "Code"
        ) != "ConditionalCheckFailedException":
            raise


# ---------- 分布式 leader 锁(P1-4:reconcile/暖池 loop 单实例) ----------
# 复用 lease 的"条件写 + TTL 过期"模式,但作用于独立的 LOCKS_TABLE 单条 item,
# 语义是"全局单 leader",区别于 per-sandbox 的 acquire_lease。

def acquire_leader_lock(lock_id: str, owner: str, ttl_s: int = 30) -> int | None:
    """
    抢占/续持 leader 锁。成功返回 rvn(record version number,fencing token),
    失败(已被他人持有且未过期)返回 None。

    条件:锁不存在 OR 已过期 OR owner 就是自己(续租)。
    每次成功写都自增 rvn —— 后续自动重调度可用它做 fencing。
    """
    now     = _utcnow()
    expires = _utcnow_plus(ttl_s)
    try:
        resp = _locks().update_item(
            Key={"lock_id": lock_id},
            UpdateExpression="SET #o = :owner, expires = :exp ADD rvn :one",
            ConditionExpression=(
                "attribute_not_exists(lock_id) OR expires < :now OR #o = :owner"
            ),
            ExpressionAttributeNames={"#o": "owner"},
            ExpressionAttributeValues={
                ":owner": owner, ":exp": expires, ":now": now, ":one": 1,
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["rvn"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise


def renew_leader_lock(lock_id: str, owner: str, ttl_s: int = 30) -> int | None:
    """续租(仅当自己仍是 owner)。语义等同 acquire 的续租分支,单列以示意图。"""
    return acquire_leader_lock(lock_id, owner, ttl_s)


def release_leader_lock(lock_id: str, owner: str) -> None:
    try:
        _locks().delete_item(
            Key={"lock_id": lock_id},
            ConditionExpression="#o = :owner",
            ExpressionAttributeNames={"#o": "owner"},
            ExpressionAttributeValues={":owner": owner},
        )
    except Exception:
        pass


# ---------- Warm Pool ----------

def mark_warm(sandbox_id: str) -> None:
    force_update(sandbox_id, {"pool_state": "warm", "state": "warm", "driver": os.environ.get("SANDBOX_DRIVER", "firecracker")})


def _warm_items(driver: str, pool: str | None = None) -> list[dict]:
    """List warm entries for one placement pool using the existing warm GSI."""
    items: list[dict] = []
    kwargs: dict = {
        "IndexName": "pool_state-driver-index",
        "KeyConditionExpression": "pool_state = :w AND driver = :d",
        "ExpressionAttributeValues": {":w": "warm", ":d": driver},
    }
    while True:
        resp = _sb().query(**kwargs)
        items.extend(
            item for item in resp.get("Items", [])
            if (item.get("pool") or None) == pool
        )
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def claim_warm_item(driver: str, pool: str | None = None) -> str | None:
    # Conditional update fences two API/operator replicas racing for the same
    # warm entry. Continue through candidates if another replica wins first.
    for item in _warm_items(driver, pool):
        candidate = item["id"]
        try:
            _sb().update_item(
                Key={"id": candidate},
                UpdateExpression="SET pool_state = :claimed",
                ConditionExpression=(
                    "pool_state = :warm AND "
                    "(#pool = :pool OR "
                    "(attribute_not_exists(#pool) AND :pool = :empty))"
                ),
                ExpressionAttributeNames={"#pool": "pool"},
                ExpressionAttributeValues={
                    ":claimed": "claimed",
                    ":warm": "warm",
                    ":pool": pool or "",
                    ":empty": "",
                },
            )
            return candidate
        except ClientError as exc:
            if exc.response.get("Error", {}).get(
                "Code"
            ) == "ConditionalCheckFailedException":
                continue
            raise
    return None


def count_warm(driver: str, pool: str | None = None) -> int:
    return len(_warm_items(driver, pool))


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


def list_events(sandbox_id: str | None = None, limit: int = 100) -> list[dict]:
    """
    读事件历史,供 Dashboard 时间线展示。
      - sandbox_id 给定 → query 单沙盒事件(走主键 id,最新在前)。
      - sandbox_id 为空 → 全局时间线:全表 scan 后按 ts 倒序取 limit 条
        (POC 规模数百沙盒 × 少量事件可接受;量大后应建 GSI 或改按时间分区,
         同 list_by_states 的取舍)。
    """
    if sandbox_id:
        resp = _ev().query(
            KeyConditionExpression="id = :i",
            ExpressionAttributeValues={":i": sandbox_id},
            ScanIndexForward=False,  # ts 倒序,最新在前
            Limit=limit,
        )
        return [_from_dynamo(i) for i in resp.get("Items", [])]

    items: list[dict] = []
    kwargs: dict = {}
    while True:
        resp = _ev().scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    items.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return [_from_dynamo(i) for i in items[:limit]]


# ---------- 内部工具 ----------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow_plus(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _utcnow_minus(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _dt_now() -> datetime:
    return datetime.now(timezone.utc)
