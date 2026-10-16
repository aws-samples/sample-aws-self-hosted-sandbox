#!/usr/bin/env python3
"""Route A unit/integration tests without a real Kubernetes cluster."""
from __future__ import annotations

import copy
import os
import sys
import threading
import unittest
from datetime import datetime, timezone

import boto3
from moto import mock_aws

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

os.environ.update({
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "DYNAMODB_TABLE": "sandboxes",
    "DYNAMODB_EVENTS_TABLE": "sandbox_events",
    "DYNAMODB_TAPIDX_TABLE": "sandbox_tap_idx",
    "DYNAMODB_NODES_TABLE": "sandbox_nodes",
    "DYNAMODB_LOCKS_TABLE": "sandbox_locks",
    "ALLOW_UNAUTHENTICATED": "1",
    "CRD_CONTROL_ENABLED": "1",
    "AUTO_SNAPSHOT_BASE": "0",
    "WARM_POOL_SIZE": "0",
})

from sandbox_api import db
from sandbox_api.crd import (
    CRDCreateOutcomeUnknown,
    FINALIZER,
    FirecrackerSandboxStore,
    object_from_record,
)
from sandbox_api.operator import FirecrackerSandboxOperator


def _create_tables() -> None:
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="sandboxes",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "tenant_id", "AttributeType": "S"},
            {"AttributeName": "updated_at", "AttributeType": "S"},
            {"AttributeName": "idempotency_key", "AttributeType": "S"},
            {"AttributeName": "pool_state", "AttributeType": "S"},
            {"AttributeName": "driver", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "tenant_id-updated_at-index",
                "KeySchema": [
                    {"AttributeName": "tenant_id", "KeyType": "HASH"},
                    {"AttributeName": "updated_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "idempotency_key-index",
                "KeySchema": [{
                    "AttributeName": "idempotency_key",
                    "KeyType": "HASH",
                }],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "pool_state-driver-index",
                "KeySchema": [
                    {"AttributeName": "pool_state", "KeyType": "HASH"},
                    {"AttributeName": "driver", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    ddb.create_table(
        TableName="sandbox_events",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "id", "KeyType": "HASH"},
            {"AttributeName": "ts", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "ts", "AttributeType": "S"},
        ],
    )
    for table_name, key in (
        ("sandbox_tap_idx", "node"),
        ("sandbox_nodes", "node_id"),
        ("sandbox_locks", "lock_id"),
    ):
        ddb.create_table(
            TableName=table_name,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": key, "AttributeType": "S"}
            ],
        )


def _record(sid: str, state: str = "creating") -> dict:
    now = db._utcnow()
    return {
        "id": sid,
        "tenant_id": "tenant-a",
        "state": state,
        "driver": "firecracker",
        "image": "min",
        "cpu": 2,
        "mem_mib": 512,
        "env": {"A": "B"},
        "services": [{
            "port": 8080,
            "protocol": "tcp",
            "autostop": True,
            "autostart": True,
        }],
        "meta": {"auto_sleep": True, "auto_wake": True},
        "pool": "",
        "created_at": now,
        "updated_at": now,
        "last_active_at": now,
    }


class FakeStore:
    def __init__(self):
        self.resources: dict[str, dict] = {}
        self.on_change = None

    def ready(self):
        return True

    def create(self, record):
        resource = object_from_record(record)
        resource["metadata"]["generation"] = 1
        resource["metadata"]["creationTimestamp"] = datetime.now(
            timezone.utc
        ).isoformat()
        self.resources[record["id"]] = resource
        if self.on_change:
            self.on_change(resource)
        return copy.deepcopy(resource)

    def create_confirmed(self, record):
        return self.create(record)

    def ensure(self, record):
        current = self.resources.get(record["id"])
        if current:
            return copy.deepcopy(current), False
        return self.create(record), True

    def get(self, sid):
        resource = self.resources.get(sid)
        return copy.deepcopy(resource) if resource else None

    def list(self, limit=None):
        values = list(self.resources.values())
        if limit is not None:
            values = values[:limit]
        return copy.deepcopy(values)

    def request_state(
        self,
        sid,
        desired_state,
        operation_id,
        *,
        suspend_reason="",
        resource_version="",
    ):
        resource = self.resources[sid]
        resource["spec"]["desiredState"] = desired_state
        resource["spec"]["operationId"] = operation_id
        resource["metadata"]["generation"] += 1
        if suspend_reason:
            resource["spec"]["suspendReason"] = suspend_reason
        else:
            resource["spec"].pop("suspendReason", None)
        if self.on_change:
            self.on_change(copy.deepcopy(resource))
        return copy.deepcopy(resource)

    def patch_status(
        self,
        sid,
        record,
        *,
        observed_generation=None,
        observed_operation_id=None,
        conditions=None,
    ):
        from sandbox_api.crd import status_from_record
        self.resources[sid]["status"] = status_from_record(
            record,
            observed_generation=observed_generation,
            observed_operation_id=observed_operation_id,
            conditions=conditions,
        )
        return copy.deepcopy(self.resources[sid])

    def delete(self, sid):
        resource = self.resources.get(sid)
        if not resource:
            return
        resource["metadata"]["deletionTimestamp"] = db._utcnow()
        if self.on_change:
            self.on_change(copy.deepcopy(resource))

    def remove_finalizer(self, sid, resource):
        current = self.resources.get(sid)
        if not current:
            return
        current["metadata"]["finalizers"] = []
        if current["metadata"].get("deletionTimestamp"):
            self.resources.pop(sid, None)

    def ensure_finalizer(self, sid, resource):
        current = self.resources[sid]
        finalizers = current["metadata"].setdefault("finalizers", [])
        if FINALIZER not in finalizers:
            finalizers.append(FINALIZER)


class FakeDriver:
    def __init__(self):
        self.running: dict[str, bool] = {}
        self.calls: list[tuple] = []
        self._next_tap = 10

    def create(self, sid, spec, pool=None):
        self.calls.append(("create", sid, pool))
        self.running[sid] = True
        self._next_tap += 1
        return {
            "node": "10.0.0.10",
            "tap_idx": self._next_tap,
            "guest_ip": f"172.18.{self._next_tap}.2",
        }

    def suspend(self, sid, record):
        self.calls.append(("suspend", sid))
        self.running[sid] = False
        return {
            "snapshot_type": "diff",
            "snapshot_size_bytes": 1024,
            "snapshot_actual_bytes": 256,
            "snapshot_s3": f"s3://bucket/sbx/{sid}/",
        }

    def resume(self, sid, record, snapshot_id=None):
        self.calls.append(("resume", sid, snapshot_id))
        self.running[sid] = True
        return {
            "node": "10.0.0.10",
            "guest_ip": record.get("guest_ip", "172.18.11.2"),
            "restore_mode": "local",
            "restore_time_s": 0.01,
        }

    def destroy(self, sid, record):
        self.calls.append(("destroy", sid))
        self.running.pop(sid, None)

    def get_runtime_state(self, sid, record):
        return "running" if self.running.get(sid) else "stopped"


class FailingSuspendDriver(FakeDriver):
    def suspend(self, sid, record):
        self.calls.append(("suspend", sid))
        raise RuntimeError("snapshot failed")


class FinalizerRaceStore(FakeStore):
    def remove_finalizer(self, sid, resource):
        self.resources.pop(sid, None)
        raise RuntimeError("another replica already removed the finalizer")


class DelayedDeleteStore(FakeStore):
    def delete(self, sid):
        resource = self.resources.get(sid)
        if not resource:
            return
        resource["metadata"]["deletionTimestamp"] = db._utcnow()
        if self.on_change:
            snapshot = copy.deepcopy(resource)
            threading.Timer(
                0.05, self.on_change, args=(snapshot,)
            ).start()


class UnknownCreateStore(FakeStore):
    def create_confirmed(self, record):
        raise CRDCreateOutcomeUnknown(
            "CRD create outcome could not be confirmed"
        )


class TestCRDMapping(unittest.TestCase):
    def test_crd_preserves_public_spec_and_finalizer(self):
        resource = object_from_record(_record("abc12345"))
        self.assertEqual(resource["kind"], "FirecrackerSandbox")
        self.assertEqual(resource["spec"]["desiredState"], "Running")
        self.assertEqual(resource["spec"]["memoryMiB"], 512)
        self.assertEqual(resource["spec"]["env"], {"A": "B"})
        self.assertEqual(resource["metadata"]["finalizers"], [FINALIZER])

    def test_delete_uses_background_propagation(self):
        class CaptureApi:
            body = None

            def delete_namespaced_custom_object(
                self, _group, _version, _namespace, _plural, _sid, *, body
            ):
                self.body = body

        api = CaptureApi()
        FirecrackerSandboxStore(api).delete("abc12345")
        self.assertEqual(api.body.propagation_policy, "Background")

    def test_create_confirms_object_after_response_is_lost(self):
        class ResponseLostApi:
            resource = None

            def create_namespaced_custom_object(
                self, _group, _version, _namespace, _plural, body
            ):
                self.resource = copy.deepcopy(body)
                raise ConnectionError("response lost")

            def get_namespaced_custom_object(
                self, _group, _version, _namespace, _plural, _sid
            ):
                return copy.deepcopy(self.resource)

        record = _record("lostresp")
        created = FirecrackerSandboxStore(
            ResponseLostApi()
        ).create_confirmed(record)
        self.assertEqual(created["metadata"]["name"], "lostresp")

    def test_create_unknown_outcome_has_distinct_error(self):
        class UnreachableApi:
            def create_namespaced_custom_object(self, *_args, **_kwargs):
                raise ConnectionError("write unavailable")

            def get_namespaced_custom_object(self, *_args, **_kwargs):
                raise ConnectionError("read unavailable")

        with self.assertRaises(CRDCreateOutcomeUnknown):
            FirecrackerSandboxStore(
                UnreachableApi()
            ).create_confirmed(_record("unknown1"))


class TestOperatorLifecycle(unittest.TestCase):
    @mock_aws
    def test_full_lifecycle_and_manual_idle_distinction(self):
        _create_tables()
        store = FakeStore()
        driver = FakeDriver()
        operator = FirecrackerSandboxOperator(store, driver)
        operator.warm_pool.claim = lambda *_args, **_kwargs: False

        record = _record("routea01")
        db.put(record)
        resource = store.create(record)
        operator.reconcile(resource)
        self.assertEqual(db.get("routea01")["state"], "running")
        self.assertEqual(store.get("routea01")["status"]["phase"], "running")

        store.request_state(
            "routea01", "Suspended", "manual-op",
            suspend_reason="manual",
        )
        operator.reconcile(store.get("routea01"))
        self.assertEqual(db.get("routea01")["state"], "suspended")

        store.request_state("routea01", "Running", "resume-op")
        operator.reconcile(store.get("routea01"))
        self.assertEqual(db.get("routea01")["state"], "running")

        db.force_update(
            "routea01", {"last_active_at": "2000-01-01T00:00:00+00:00"}
        )
        store.request_state(
            "routea01", "Suspended", "idle-op",
            suspend_reason="idle",
        )
        operator.reconcile(store.get("routea01"))
        self.assertEqual(db.get("routea01")["state"], "slept")

        store.delete("routea01")
        operator.reconcile(store.get("routea01"))
        self.assertIsNone(db.get("routea01"))
        self.assertIsNone(store.get("routea01"))
        self.assertEqual(
            [call[0] for call in driver.calls],
            ["create", "suspend", "resume", "suspend", "destroy"],
        )

    @mock_aws
    def test_suspend_recovery_waits_for_active_operation_lease(self):
        _create_tables()
        store = FakeStore()
        driver = FakeDriver()
        driver.running["lease-suspend"] = False
        record = _record("lease-suspend", "suspending")
        db.put(record)
        resource = store.create(record)
        store.request_state(
            "lease-suspend",
            "Suspended",
            "manual-op",
            suspend_reason="manual",
        )
        operator = FirecrackerSandboxOperator(store, driver)

        active_lease = db.acquire_lease(
            "lease-suspend",
            duration_s=300,
        )
        operator.reconcile(store.get("lease-suspend"))
        self.assertEqual(db.get("lease-suspend")["state"], "suspending")

        db.release_lease("lease-suspend", active_lease)
        operator.reconcile(store.get("lease-suspend"))
        self.assertEqual(db.get("lease-suspend")["state"], "suspended")
        self.assertEqual(
            store.get("lease-suspend")["status"]["phase"],
            "suspended",
        )

    @mock_aws
    def test_delete_finalizer_race_does_not_recreate_projection(self):
        _create_tables()
        store = FinalizerRaceStore()
        driver = FakeDriver()
        record = _record("delete-race", "running")
        db.put(record)
        resource = store.create(record)
        store.delete("delete-race")
        operator = FirecrackerSandboxOperator(store, driver)

        operator.reconcile(store.get("delete-race"))

        self.assertIsNone(db.get("delete-race"))
        self.assertIsNone(store.get("delete-race"))
        self.assertEqual(driver.calls, [("destroy", "delete-race")])

    @mock_aws
    def test_adopts_existing_runtime_without_recreating_it(self):
        _create_tables()
        store = FakeStore()
        driver = FakeDriver()
        driver.running["legacy01"] = True
        db.put(_record("legacy01", "running"))

        operator = FirecrackerSandboxOperator(store, driver)
        stats = operator.adopt_legacy_records()

        self.assertEqual(stats["created"], 1)
        self.assertEqual(store.get("legacy01")["status"]["phase"], "running")
        self.assertNotIn("create", [call[0] for call in driver.calls])

    @mock_aws
    def test_stale_running_snapshot_is_not_auto_restored(self):
        _create_tables()
        store = FakeStore()
        driver = FakeDriver()
        record = {
            **_record("stale001", "running"),
            "node": "10.0.0.99",
            "tap_idx": 42,
            "snapshot_s3": "s3://bucket/old-checkpoint/",
        }
        db.put(record)
        resource = store.create(record)
        operator = FirecrackerSandboxOperator(store, driver)
        operator.reconcile(resource)

        self.assertEqual(db.get("stale001")["state"], "orphaned")
        self.assertNotIn("resume", [call[0] for call in driver.calls])

    @mock_aws
    def test_renew_lease_keeps_long_operation_fenced(self):
        _create_tables()
        db.put(_record("lease001"))
        lease = db.acquire_lease("lease001", duration_s=1)
        self.assertTrue(db.renew_lease("lease001", lease, duration_s=30))
        with self.assertRaises(Exception):
            db.acquire_lease("lease001")
        db.release_lease("lease001", lease)
        self.assertTrue(db.acquire_lease("lease001"))

    @mock_aws
    def test_acquire_lease_never_recreates_deleted_projection(self):
        _create_tables()
        with self.assertRaises(Exception):
            db.acquire_lease("already-deleted")
        self.assertIsNone(db.get("already-deleted"))

    @mock_aws
    def test_idle_suspend_is_cancelled_when_activity_arrives(self):
        _create_tables()
        store = FakeStore()
        driver = FakeDriver()
        driver.running["active01"] = True
        record = {
            **_record("active01", "running"),
            "node": "10.0.0.10",
            "tap_idx": 11,
            "guest_ip": "172.18.11.2",
        }
        db.put(record)
        store.create(record)
        store.request_state(
            "active01", "Suspended", "idle-race",
            suspend_reason="idle",
        )

        operator = FirecrackerSandboxOperator(store, driver)
        operator.reconcile(store.get("active01"))

        self.assertEqual(db.get("active01")["state"], "running")
        self.assertEqual(
            store.get("active01")["spec"]["desiredState"], "Running"
        )
        self.assertNotIn("suspend", [call[0] for call in driver.calls])


class TestAPIBridge(unittest.TestCase):
    @mock_aws
    def test_existing_api_contract_is_served_through_operator(self):
        _create_tables()
        from sandbox_api import app

        store = FakeStore()
        driver = FakeDriver()
        operator = FirecrackerSandboxOperator(store, driver)
        operator.warm_pool.claim = lambda *_args, **_kwargs: False
        store.on_change = operator.reconcile

        old_enabled, old_store = app._CRD_CONTROL_ENABLED, app._crd_store
        app._CRD_CONTROL_ENABLED = True
        app._crd_store = store
        try:
            code, created = app.create_sandbox({
                "tenant_id": "tenant-a",
                "image": "min",
                "cpu": 2,
                "mem_mib": 512,
                "services": [{"port": 8080}],
            })
            self.assertEqual(code, 201)
            self.assertEqual(created["state"], "running")
            sid = created["id"]

            code, suspended = app.suspend_sandbox(sid)
            self.assertEqual((code, suspended["state"]), (200, "suspended"))

            code, resumed = app.resume_sandbox(sid)
            self.assertEqual((code, resumed["state"]), (200, "running"))

            code, deleted = app.destroy_sandbox(sid)
            self.assertEqual(code, 200)
            self.assertTrue(deleted["deleted"])
            self.assertIsNone(db.get(sid))
        finally:
            app._CRD_CONTROL_ENABLED = old_enabled
            app._crd_store = old_store

    @mock_aws
    def test_suspend_failure_returns_without_waiting_full_timeout(self):
        _create_tables()
        from sandbox_api import app

        store = FakeStore()
        driver = FailingSuspendDriver()
        operator = FirecrackerSandboxOperator(store, driver)
        operator.warm_pool.claim = lambda *_args, **_kwargs: False
        store.on_change = operator.reconcile

        record = {
            **_record("fail0001", "running"),
            "node": "10.0.0.10",
            "tap_idx": 11,
            "guest_ip": "172.18.11.2",
        }
        db.put(record)
        driver.running["fail0001"] = True
        store.create(record)

        old_enabled, old_store = app._CRD_CONTROL_ENABLED, app._crd_store
        app._CRD_CONTROL_ENABLED = True
        app._crd_store = store
        try:
            code, result = app.suspend_sandbox("fail0001")
            self.assertEqual(code, 500)
            self.assertEqual(result["state"], "running")
            self.assertEqual(
                result["failed_operation_id"],
                store.get("fail0001")["spec"]["operationId"],
            )
        finally:
            app._CRD_CONTROL_ENABLED = old_enabled
            app._crd_store = old_store

    @mock_aws
    def test_unknown_crd_create_outcome_preserves_projection(self):
        _create_tables()
        from sandbox_api import app

        store = UnknownCreateStore()
        old_enabled, old_store = app._CRD_CONTROL_ENABLED, app._crd_store
        app._CRD_CONTROL_ENABLED = True
        app._crd_store = store
        try:
            code, result = app.create_sandbox({
                "tenant_id": "tenant-a",
                "idempotency_key": "unknown-create",
            })
            self.assertEqual(code, 503)
            self.assertTrue(result["retryable"])
            self.assertIsNotNone(db.get(result["id"]))
            self.assertEqual(
                db.get_by_idempotency_key("unknown-create")["id"],
                result["id"],
            )
        finally:
            app._CRD_CONTROL_ENABLED = old_enabled
            app._crd_store = old_store

    @mock_aws
    def test_delete_waits_when_sandbox_was_already_failed(self):
        _create_tables()
        from sandbox_api import app

        store = DelayedDeleteStore()
        driver = FakeDriver()
        operator = FirecrackerSandboxOperator(store, driver)
        record = {
            **_record("failed-delete", "failed"),
            "error": "earlier resume failed",
        }
        db.put(record)
        store.create(record)
        store.on_change = operator.reconcile

        old_enabled, old_store = app._CRD_CONTROL_ENABLED, app._crd_store
        app._CRD_CONTROL_ENABLED = True
        app._crd_store = store
        try:
            code, result = app.destroy_sandbox("failed-delete")
            self.assertEqual(code, 200)
            self.assertTrue(result["deleted"])
            self.assertIsNone(db.get("failed-delete"))
        finally:
            app._CRD_CONTROL_ENABLED = old_enabled
            app._crd_store = old_store


if __name__ == "__main__":
    unittest.main(verbosity=2)
