"""
FirecrackerSandbox CRD client.

The CRD is the lifecycle source of truth in Route A:

* ``spec.desiredState`` expresses the requested lifecycle state.
* the operator calls the existing FirecrackerDriver/node-agent implementation.
* ``status`` records the observed state.
* DynamoDB remains a compatibility projection for the existing REST/Portal
  contract, idempotency indexes, events, activity signals, and node registry.

The Kubernetes client is initialized lazily so importing the REST API in local
tests does not require a kubeconfig.
"""
from __future__ import annotations

import copy
import os
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException


GROUP = os.environ.get("CRD_GROUP", "sandbox.memorion.ai")
VERSION = os.environ.get("CRD_VERSION", "v1alpha1")
PLURAL = os.environ.get("CRD_PLURAL", "firecrackersandboxes")
KIND = os.environ.get("CRD_KIND", "FirecrackerSandbox")
NAMESPACE = os.environ.get("CRD_NAMESPACE", "sandbox-system")
FINALIZER = os.environ.get(
    "CRD_FINALIZER", f"{GROUP}/runtime-cleanup"
)


class CRDCreateOutcomeUnknown(RuntimeError):
    """The API request failed and a follow-up read could not confirm outcome."""


def crd_control_enabled() -> bool:
    return os.environ.get("CRD_CONTROL_ENABLED", "0").strip().lower() in {
        "1", "true", "yes",
    }


def desired_state_for_record(record: dict) -> tuple[str, str]:
    """Return ``(desiredState, suspendReason)`` when adopting a legacy record."""
    state = record.get("state", "")
    if state == "slept":
        return "Suspended", "idle"
    if state in {"suspended", "suspending"}:
        return "Suspended", "manual"
    return "Running", ""


def object_from_record(record: dict) -> dict:
    """Build a CR from the existing public/DynamoDB record shape."""
    desired, reason = desired_state_for_record(record)
    spec: dict[str, Any] = {
        "desiredState": desired,
        "tenantId": record.get("tenant_id", "default"),
        "image": record.get("image", ""),
        "cpu": int(record.get("cpu", 2)),
        "memoryMiB": int(record.get("mem_mib", 4096)),
        "env": copy.deepcopy(record.get("env", {})),
        "services": copy.deepcopy(record.get("services", [])),
        "meta": copy.deepcopy(record.get("meta", {})),
        "pool": record.get("pool", ""),
        # Every API lifecycle request replaces this id. It makes repeated
        # requests observable even when desiredState itself is unchanged.
        "operationId": record.get("operation_id", ""),
    }
    if reason:
        spec["suspendReason"] = reason
    metadata: dict[str, Any] = {
        "name": record["id"],
        "namespace": NAMESPACE,
        "finalizers": [FINALIZER],
        "labels": {
            "app.kubernetes.io/managed-by": "firecracker-operator",
            "sandbox.memorion.ai/tenant": _label_value(
                record.get("tenant_id", "default")
            ),
        },
    }
    if idem := record.get("idempotency_key"):
        metadata["annotations"] = {
            "sandbox.memorion.ai/idempotency-key": str(idem),
        }
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": KIND,
        "metadata": metadata,
        "spec": spec,
    }


def status_from_record(
    record: dict,
    *,
    observed_generation: int | None = None,
    observed_operation_id: str | None = None,
    conditions: list[dict] | None = None,
) -> dict:
    """Map the legacy public record to CR status without changing API fields."""
    status: dict[str, Any] = {
        "phase": record.get("state", "unknown"),
        "node": record.get("node", ""),
        "guestIP": record.get("guest_ip", ""),
        "tapIndex": int(record.get("tap_idx", 0) or 0),
        "snapshotType": record.get("snapshot_type", ""),
        "snapshotS3": record.get("snapshot_s3", ""),
        "snapshotSizeBytes": int(record.get("snapshot_size_bytes", 0) or 0),
        "snapshotActualBytes": int(
            record.get("snapshot_actual_bytes", 0) or 0
        ),
        "restoreMode": record.get("restore_mode", ""),
        "restoreTimeSeconds": _float_or_zero(
            record.get("restore_time_s", 0)
        ),
        "lastTransitionTime": record.get("updated_at", ""),
    }
    if observed_generation is not None:
        status["observedGeneration"] = int(observed_generation)
    if observed_operation_id is not None:
        status["observedOperationId"] = observed_operation_id
    if conditions is not None:
        status["conditions"] = conditions
    if error := record.get("error"):
        status["message"] = str(error)[:2048]
    return status


class FirecrackerSandboxStore:
    """Thin, retry-friendly wrapper around ``CustomObjectsApi``."""

    def __init__(self, api: Any | None = None):
        self._api = api

    @property
    def api(self):
        if self._api is None:
            self._load_config()
            self._api = client.CustomObjectsApi()
        return self._api

    @staticmethod
    def _load_config() -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()

    def ready(self) -> bool:
        self.list(limit=1)
        return True

    def get(self, sandbox_id: str) -> dict | None:
        try:
            return self.api.get_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, PLURAL, sandbox_id
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def list(self, *, limit: int | None = None) -> list[dict]:
        page_size = limit or int(os.environ.get("CRD_LIST_PAGE_SIZE", "500"))
        items: list[dict] = []
        continue_token = ""
        while True:
            kwargs: dict[str, Any] = {"limit": page_size}
            if continue_token:
                kwargs["_continue"] = continue_token
            result = self.api.list_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, PLURAL, **kwargs
            )
            items.extend(result.get("items", []))
            if limit is not None and len(items) >= limit:
                return items[:limit]
            continue_token = str(
                (result.get("metadata") or {}).get("continue", "")
            )
            if not continue_token:
                return items

    def create(self, record: dict) -> dict:
        return self.api.create_namespaced_custom_object(
            GROUP, VERSION, NAMESPACE, PLURAL, object_from_record(record)
        )

    def create_confirmed(self, record: dict) -> dict:
        """Create a CR, resolving the common response-lost ambiguity.

        If Kubernetes committed the object but the client lost the response,
        a follow-up GET turns the request into a successful, idempotent create.
        When both the write and confirmation read fail, callers must preserve
        their compatibility projection because deleting it could hide a CR
        that the operator is already reconciling.
        """
        try:
            return self.create(record)
        except Exception as create_exc:
            try:
                current = self.get(record["id"])
            except Exception as confirm_exc:
                raise CRDCreateOutcomeUnknown(
                    "CRD create outcome could not be confirmed"
                ) from confirm_exc
            if current is not None:
                return current
            raise create_exc

    def ensure(self, record: dict) -> tuple[dict, bool]:
        existing = self.get(record["id"])
        if existing is not None:
            return existing, False
        try:
            created = self.create_confirmed(record)
            return created, True
        except ApiException as exc:
            if exc.status == 409:
                current = self.get(record["id"])
                if current is not None:
                    return current, False
            raise

    def request_state(
        self,
        sandbox_id: str,
        desired_state: str,
        operation_id: str,
        *,
        suspend_reason: str = "",
        resource_version: str = "",
    ) -> dict:
        spec: dict[str, Any] = {
            "desiredState": desired_state,
            "operationId": operation_id,
        }
        if suspend_reason:
            spec["suspendReason"] = suspend_reason
        elif desired_state == "Running":
            # JSON merge patch null removes a stale suspend reason.
            spec["suspendReason"] = None
        patch: dict[str, Any] = {"spec": spec}
        if resource_version:
            patch["metadata"] = {"resourceVersion": resource_version}
        return self.api.patch_namespaced_custom_object(
            GROUP, VERSION, NAMESPACE, PLURAL, sandbox_id, patch
        )

    def patch_status(
        self,
        sandbox_id: str,
        record: dict,
        *,
        observed_generation: int | None = None,
        observed_operation_id: str | None = None,
        conditions: list[dict] | None = None,
    ) -> dict:
        desired_status = status_from_record(
            record,
            observed_generation=observed_generation,
            observed_operation_id=observed_operation_id,
            conditions=conditions,
        )
        current = self.get(sandbox_id)
        if current is not None and _status_equivalent(
            current.get("status", {}), desired_status
        ):
            return current
        return self.api.patch_namespaced_custom_object_status(
            GROUP,
            VERSION,
            NAMESPACE,
            PLURAL,
            sandbox_id,
            {"status": desired_status},
        )

    def delete(self, sandbox_id: str) -> None:
        try:
            self.api.delete_namespaced_custom_object(
                GROUP,
                VERSION,
                NAMESPACE,
                PLURAL,
                sandbox_id,
                body=client.V1DeleteOptions(
                    # The CR's own runtime cleanup finalizer is authoritative.
                    # Foreground propagation tries to add Kubernetes'
                    # foregroundDeletion finalizer and can race a repeated
                    # delete after deletionTimestamp is already set.
                    propagation_policy="Background",
                    grace_period_seconds=0,
                ),
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    def remove_finalizer(self, sandbox_id: str, resource: dict) -> None:
        finalizers = list(resource.get("metadata", {}).get("finalizers") or [])
        if FINALIZER not in finalizers:
            return
        finalizers.remove(FINALIZER)
        try:
            self.api.patch_namespaced_custom_object(
                GROUP,
                VERSION,
                NAMESPACE,
                PLURAL,
                sandbox_id,
                {"metadata": {"finalizers": finalizers}},
            )
        except ApiException as exc:
            # Another operator replica may have removed the same finalizer
            # after this worker deleted the DynamoDB projection. The CR being
            # gone is the desired, idempotent outcome.
            if exc.status != 404:
                raise

    def ensure_finalizer(self, sandbox_id: str, resource: dict) -> None:
        finalizers = list(resource.get("metadata", {}).get("finalizers") or [])
        if FINALIZER in finalizers:
            return
        finalizers.append(FINALIZER)
        self.api.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            NAMESPACE,
            PLURAL,
            sandbox_id,
            {"metadata": {"finalizers": finalizers}},
        )

    def watch_kwargs(self) -> dict[str, Any]:
        return {
            "group": GROUP,
            "version": VERSION,
            "namespace": NAMESPACE,
            "plural": PLURAL,
        }


def _label_value(value: Any) -> str:
    # Kubernetes labels are deliberately lossy metadata. The authoritative
    # tenant id remains in spec/DynamoDB.
    raw = str(value or "default").lower()
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in raw)
    return (safe.strip("._-") or "default")[:63]


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _status_equivalent(current: dict, desired: dict) -> bool:
    """Ignore condition timestamps so an unchanged reconcile is a no-op."""
    def normalized(value: dict) -> dict:
        result = copy.deepcopy(value)
        for condition in result.get("conditions", []) or []:
            condition.pop("lastTransitionTime", None)
        return result

    return normalized(current) == normalized(desired)
