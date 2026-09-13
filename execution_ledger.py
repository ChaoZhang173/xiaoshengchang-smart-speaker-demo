#!/usr/bin/env python3
"""Thread-safe, in-memory execution audit trail.

The ledger intentionally stores only bounded, redacted snapshots.  It is useful
for coordinating tool calls inside the demo, but is not a durable audit log and
must not be treated as a credential store.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import re
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


RUNNING = "running"
SUCCEEDED = "succeeded"
DISPATCHED = "dispatched"
FAILED = "failed"
BLOCKED = "blocked"
AWAITING_CONFIRMATION = "awaiting_confirmation"

VALID_STATUSES = frozenset(
    {RUNNING, SUCCEEDED, DISPATCHED, FAILED, BLOCKED, AWAITING_CONFIRMATION}
)
TERMINAL_STATUSES = frozenset({SUCCEEDED, DISPATCHED, FAILED, BLOCKED})

DEFAULT_MAX_RECORDS = 500
DEFAULT_MAX_SNAPSHOT_BYTES = 16 * 1024
DEFAULT_MAX_STRING_CHARS = 4_000
DEFAULT_MAX_COLLECTION_ITEMS = 100
DEFAULT_MAX_DEPTH = 8
REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"

_OPERATION_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_OBVIOUS_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"((?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|token|"
        r"password|authorization|cookie|client[ _-]?secret)\s*[:=]\s*)"
        r"[^\s,;]+",
        re.IGNORECASE,
    ),
)
_SENSITIVE_KEY_WORDS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "passwd",
        "password",
        "pwd",
        "secret",
        "token",
    }
)
_SENSITIVE_COMPACT_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authkey",
        "authorization",
        "clientsecret",
        "cookie",
        "idtoken",
        "key",
        "password",
        "privatekey",
        "refreshtoken",
        "secretkey",
        "sessioncookie",
        "token",
    }
)


class ExecutionLedgerError(RuntimeError):
    """Base class for execution-ledger failures."""


class ExecutionNotFoundError(ExecutionLedgerError, KeyError):
    """Raised when an execution id is not present in the bounded ledger."""


class InvalidExecutionTransition(ExecutionLedgerError):
    """Raised when a terminal execution is changed or a transition is invalid."""


class ExecutionValidationError(ExecutionLedgerError, ValueError):
    """Raised for malformed operation names, ids, or configuration."""


class ExecutionCapacityError(ExecutionLedgerError):
    """Raised instead of evicting an execution that is still active."""


@dataclass(frozen=True)
class ExecutionRecord:
    """A point-in-time snapshot of one tool or workflow execution."""

    execution_id: str
    operation: str
    status: str
    arguments: Any
    metadata: Any
    result: Any
    error: Any
    reason: Any
    started_at: str
    updated_at: str
    finished_at: Optional[str]
    has_idempotency_key: bool

    def to_dict(self) -> Dict[str, Any]:
        """Return a detached dictionary safe for caller mutation."""

        return copy.deepcopy(asdict(self))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _clean_operation(value: Any) -> str:
    if not isinstance(value, str):
        raise ExecutionValidationError("operation must be a string")
    operation = value.strip()
    if not _OPERATION_RE.fullmatch(operation):
        raise ExecutionValidationError(
            "operation must be 1-128 characters using letters, digits, '.', '_', "
            "':', '/', or '-'"
        )
    return operation


def _clean_execution_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionValidationError("execution_id must be a non-empty string")
    return value.strip()


def _idempotency_digest(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionValidationError("idempotency_key must be a string")
    value = value.strip()
    if not value:
        raise ExecutionValidationError("idempotency_key must not be empty")
    if len(value) > 512:
        raise ExecutionValidationError("idempotency_key must be at most 512 characters")
    # Keep the opaque caller value out of records; it may itself contain a secret.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sensitive_key(key: str) -> bool:
    separated = _CAMEL_BOUNDARY_RE.sub("_", key).casefold()
    words = [part for part in _NON_WORD_RE.split(separated) if part]
    compact = "".join(words)
    if compact in _SENSITIVE_COMPACT_KEYS:
        return True
    if any(word in _SENSITIVE_KEY_WORDS for word in words):
        return True
    # Common names such as api_key, signing_key, and encryption_key.
    return bool(words and words[-1] == "key" and len(words) <= 3)


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _OBVIOUS_SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda match: match.group(1) + REDACTED, redacted)
        else:
            redacted = pattern.sub(REDACTED, redacted)
    return redacted


class _SnapshotSanitizer:
    """Convert values to bounded JSON-like snapshots without retaining objects."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_string_chars: int,
        max_collection_items: int,
        max_depth: int,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_string_chars = max_string_chars
        self.max_collection_items = max_collection_items
        self.max_depth = max_depth

    def snapshot(self, value: Any) -> Any:
        clean = self._walk(value, depth=0, ancestors=set())
        encoded = json.dumps(
            clean,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) <= self.max_bytes:
            return clean
        # A fixed-size marker makes the memory bound strict even for huge input.
        return {
            "_truncated": True,
            "_original_snapshot_bytes": len(encoded),
            "_message": TRUNCATED,
        }

    def _walk(self, value: Any, *, depth: int, ancestors: set[int]) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value if value.bit_length() <= 1024 else "[INTEGER OMITTED]"
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, str):
            clean = _redact_text(value)
            if len(clean) > self.max_string_chars:
                return clean[: self.max_string_chars] + TRUNCATED
            return clean
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "[BINARY OMITTED: %d bytes]" % len(value)
        if isinstance(value, BaseException):
            return {
                "type": type(value).__name__,
                "message": self._walk(str(value), depth=depth + 1, ancestors=ancestors),
            }
        if depth >= self.max_depth:
            return "[MAX DEPTH]"

        if isinstance(value, dict):
            identity = id(value)
            if identity in ancestors:
                return "[CYCLE]"
            ancestors.add(identity)
            try:
                clean_dict: Dict[str, Any] = {}
                entries = list(
                    itertools.islice(value.items(), self.max_collection_items)
                )
                for raw_key, raw_value in entries:
                    key = self._safe_key(raw_key)
                    if _is_sensitive_key(key):
                        clean_dict[key] = REDACTED
                    else:
                        clean_dict[key] = self._walk(
                            raw_value,
                            depth=depth + 1,
                            ancestors=ancestors,
                        )
                if len(value) > self.max_collection_items:
                    clean_dict["_truncated_items"] = len(value) - len(entries)
                return clean_dict
            finally:
                ancestors.remove(identity)

        if isinstance(value, (list, tuple, set, frozenset)):
            identity = id(value)
            if identity in ancestors:
                return "[CYCLE]"
            ancestors.add(identity)
            try:
                selected = list(itertools.islice(value, self.max_collection_items))
                clean_list = [
                    self._walk(item, depth=depth + 1, ancestors=ancestors)
                    for item in selected
                ]
                if len(value) > self.max_collection_items:
                    clean_list.append(
                        {"_truncated_items": len(value) - self.max_collection_items}
                    )
                return clean_list
            finally:
                ancestors.remove(identity)

        # Avoid calling arbitrary __repr__ or __str__, which may expose credentials.
        return "[UNSUPPORTED TYPE: %s]" % type(value).__name__

    def _safe_key(self, value: Any) -> str:
        if isinstance(value, str):
            key = value
        elif value is None or isinstance(value, (bool, int, float)):
            key = str(value)
        else:
            key = "[KEY TYPE: %s]" % type(value).__name__
        key = _redact_text(key)
        if len(key) > 200:
            key = key[:200] + TRUNCATED
        return key


class ExecutionLedger:
    """Bounded, process-local ledger for agent and tool executions."""

    def __init__(
        self,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
        max_string_chars: int = DEFAULT_MAX_STRING_CHARS,
        max_collection_items: int = DEFAULT_MAX_COLLECTION_ITEMS,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        if not isinstance(max_records, int) or not 1 <= max_records <= 100_000:
            raise ExecutionValidationError("max_records must be between 1 and 100000")
        if not isinstance(max_snapshot_bytes, int) or not 256 <= max_snapshot_bytes <= 1_048_576:
            raise ExecutionValidationError(
                "max_snapshot_bytes must be between 256 and 1048576"
            )
        if not isinstance(max_string_chars, int) or not 16 <= max_string_chars <= 100_000:
            raise ExecutionValidationError(
                "max_string_chars must be between 16 and 100000"
            )
        if not isinstance(max_collection_items, int) or not 1 <= max_collection_items <= 10_000:
            raise ExecutionValidationError(
                "max_collection_items must be between 1 and 10000"
            )
        if not isinstance(max_depth, int) or not 1 <= max_depth <= 100:
            raise ExecutionValidationError("max_depth must be between 1 and 100")

        self.max_records = max_records
        self._lock = threading.RLock()
        self._records: Dict[str, ExecutionRecord] = {}
        self._idempotency_index: Dict[str, str] = {}
        self._sanitizer = _SnapshotSanitizer(
            max_bytes=max_snapshot_bytes,
            max_string_chars=max_string_chars,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
        )

    def start(
        self,
        operation: str,
        arguments: Any = None,
        *,
        idempotency_key: Optional[str] = None,
        metadata: Any = None,
    ) -> ExecutionRecord:
        """Start an execution, atomically deduplicating an optional key."""

        operation = _clean_operation(operation)
        digest = _idempotency_digest(idempotency_key)
        with self._lock:
            if digest is not None:
                existing_id = self._idempotency_index.get(digest)
                if existing_id is not None and existing_id in self._records:
                    return self._copy_record(self._records[existing_id])

            now = _utc_now()
            execution_id = uuid.uuid4().hex
            while execution_id in self._records:
                execution_id = uuid.uuid4().hex
            record = ExecutionRecord(
                execution_id=execution_id,
                operation=operation,
                status=RUNNING,
                arguments=self._sanitizer.snapshot(
                    {} if arguments is None else arguments
                ),
                metadata=self._sanitizer.snapshot({} if metadata is None else metadata),
                result=None,
                error=None,
                reason=None,
                started_at=now,
                updated_at=now,
                finished_at=None,
                has_idempotency_key=digest is not None,
            )
            self._prune_for_insert()
            self._records[record.execution_id] = record
            if digest is not None:
                self._idempotency_index[digest] = record.execution_id
            return self._copy_record(record)

    def complete(self, execution_id: str, result: Any = None) -> ExecutionRecord:
        """Mark a running or confirmed execution as successful."""

        return self._finish(execution_id, SUCCEEDED, result=result)

    def dispatch(self, execution_id: str, result: Any = None) -> ExecutionRecord:
        """Mark an authorized client action as ready for delivery."""

        return self._finish(execution_id, DISPATCHED, result=result)

    def fail(self, execution_id: str, error: Any = None) -> ExecutionRecord:
        """Mark a running or confirmation-waiting execution as failed."""

        return self._finish(execution_id, FAILED, error=error)

    def block(self, execution_id: str, reason: Any = None) -> ExecutionRecord:
        """Mark an execution as blocked by policy, permissions, or dependencies."""

        return self._finish(execution_id, BLOCKED, reason=reason)

    def await_confirmation(
        self,
        execution_id: str,
        reason: Any = None,
    ) -> ExecutionRecord:
        """Pause a running execution until an external confirmation arrives."""

        execution_id = _clean_execution_id(execution_id)
        with self._lock:
            current = self._require(execution_id)
            if current.status != RUNNING:
                raise InvalidExecutionTransition(
                    "%s cannot transition from %s to %s"
                    % (execution_id, current.status, AWAITING_CONFIRMATION)
                )
            updated = replace(
                current,
                status=AWAITING_CONFIRMATION,
                reason=self._sanitizer.snapshot(reason),
                updated_at=_utc_now(),
            )
            self._records[execution_id] = updated
            return self._copy_record(updated)

    # A descriptive alias reads better at policy-engine call sites.
    request_confirmation = await_confirmation

    def resume(self, execution_id: str) -> ExecutionRecord:
        """Resume an execution after confirmation was granted."""

        execution_id = _clean_execution_id(execution_id)
        with self._lock:
            current = self._require(execution_id)
            if current.status != AWAITING_CONFIRMATION:
                raise InvalidExecutionTransition(
                    "%s cannot transition from %s to %s"
                    % (execution_id, current.status, RUNNING)
                )
            updated = replace(
                current,
                status=RUNNING,
                reason=None,
                updated_at=_utc_now(),
            )
            self._records[execution_id] = updated
            return self._copy_record(updated)

    def get(self, execution_id: str) -> ExecutionRecord:
        """Return a detached snapshot for one execution."""

        execution_id = _clean_execution_id(execution_id)
        with self._lock:
            return self._copy_record(self._require(execution_id))

    def recent(
        self,
        limit: int = 20,
        *,
        status: Optional[str] = None,
        operation: Optional[str] = None,
    ) -> List[ExecutionRecord]:
        """Return newest-first detached records, optionally filtered."""

        if not isinstance(limit, int) or not 1 <= limit <= 100_000:
            raise ExecutionValidationError("limit must be between 1 and 100000")
        if status is not None and status not in VALID_STATUSES:
            raise ExecutionValidationError("unknown execution status: %s" % status)
        if operation is not None:
            operation = _clean_operation(operation)
        with self._lock:
            matches: List[ExecutionRecord] = []
            for record in reversed(tuple(self._records.values())):
                if status is not None and record.status != status:
                    continue
                if operation is not None and record.operation != operation:
                    continue
                matches.append(self._copy_record(record))
                if len(matches) == limit:
                    break
            return matches

    def clear(self) -> int:
        """Forget terminal records while preserving in-flight executions."""

        with self._lock:
            removable = [
                execution_id
                for execution_id, record in self._records.items()
                if record.status in TERMINAL_STATUSES
            ]
            for execution_id in removable:
                self._records.pop(execution_id, None)
            if removable:
                removed = set(removable)
                stale_digests = [
                    digest
                    for digest, execution_id in self._idempotency_index.items()
                    if execution_id in removed
                ]
                for digest in stale_digests:
                    self._idempotency_index.pop(digest, None)
            return len(removable)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def _finish(
        self,
        execution_id: str,
        status: str,
        *,
        result: Any = None,
        error: Any = None,
        reason: Any = None,
    ) -> ExecutionRecord:
        execution_id = _clean_execution_id(execution_id)
        with self._lock:
            current = self._require(execution_id)
            if current.status in TERMINAL_STATUSES:
                raise InvalidExecutionTransition(
                    "%s is already terminal (%s)" % (execution_id, current.status)
                )
            if current.status not in {RUNNING, AWAITING_CONFIRMATION}:
                raise InvalidExecutionTransition(
                    "%s cannot transition from %s to %s"
                    % (execution_id, current.status, status)
                )
            now = _utc_now()
            updated = replace(
                current,
                status=status,
                result=(
                    self._sanitizer.snapshot(result)
                    if status in {SUCCEEDED, DISPATCHED}
                    else None
                ),
                error=self._sanitizer.snapshot(error) if status == FAILED else None,
                reason=self._sanitizer.snapshot(reason) if status == BLOCKED else None,
                updated_at=now,
                finished_at=now,
            )
            self._records[execution_id] = updated
            return self._copy_record(updated)

    def _require(self, execution_id: str) -> ExecutionRecord:
        try:
            return self._records[execution_id]
        except KeyError as exc:
            raise ExecutionNotFoundError(
                "execution not found or evicted: %s" % execution_id
            ) from exc

    def _prune_for_insert(self) -> None:
        while len(self._records) >= self.max_records:
            oldest_id = next(
                (
                    execution_id
                    for execution_id, record in self._records.items()
                    if record.status in TERMINAL_STATUSES
                ),
                None,
            )
            if oldest_id is None:
                raise ExecutionCapacityError(
                    "execution ledger is full of active records"
                )
            self._records.pop(oldest_id)
            stale_digests = [
                digest
                for digest, execution_id in self._idempotency_index.items()
                if execution_id == oldest_id
            ]
            for digest in stale_digests:
                self._idempotency_index.pop(digest, None)

    @staticmethod
    def _copy_record(record: ExecutionRecord) -> ExecutionRecord:
        return copy.deepcopy(record)


__all__ = [
    "AWAITING_CONFIRMATION",
    "BLOCKED",
    "DISPATCHED",
    "ExecutionCapacityError",
    "ExecutionLedger",
    "ExecutionLedgerError",
    "ExecutionNotFoundError",
    "ExecutionRecord",
    "ExecutionValidationError",
    "FAILED",
    "InvalidExecutionTransition",
    "REDACTED",
    "RUNNING",
    "SUCCEEDED",
    "TERMINAL_STATUSES",
    "VALID_STATUSES",
]
