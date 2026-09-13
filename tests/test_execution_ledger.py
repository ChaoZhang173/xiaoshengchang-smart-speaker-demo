import json
import sys
import threading
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from execution_ledger import (  # noqa: E402
    AWAITING_CONFIRMATION,
    BLOCKED,
    DISPATCHED,
    FAILED,
    REDACTED,
    RUNNING,
    SUCCEEDED,
    ExecutionCapacityError,
    ExecutionLedger,
    ExecutionNotFoundError,
    InvalidExecutionTransition,
)


class ExecutionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = ExecutionLedger()

    def test_start_and_terminal_transitions(self):
        first = self.ledger.start("calendar.create", {"title": "牙医"})
        second = self.ledger.start("weather.read", {"city": "上海"})

        self.assertNotEqual(first.execution_id, second.execution_id)
        self.assertEqual(first.status, RUNNING)
        self.assertIsNone(first.finished_at)

        completed = self.ledger.complete(first.execution_id, {"event_id": "evt-1"})
        failed = self.ledger.fail(second.execution_id, ValueError("network down"))
        self.assertEqual(completed.status, SUCCEEDED)
        self.assertEqual(completed.result, {"event_id": "evt-1"})
        self.assertIsNotNone(completed.finished_at)
        self.assertEqual(failed.status, FAILED)
        self.assertEqual(failed.error["type"], "ValueError")
        self.assertEqual(failed.error["message"], "network down")

        action = self.ledger.start("music.play", {"station": "fixed"})
        dispatched = self.ledger.dispatch(action.execution_id, {"action_count": 1})
        self.assertEqual(dispatched.status, DISPATCHED)
        self.assertEqual(dispatched.result, {"action_count": 1})

    def test_optional_idempotency_key_deduplicates_atomically(self):
        first = self.ledger.start(
            "mail.summarize",
            {"folder": "inbox"},
            idempotency_key="request-123",
        )
        duplicate = self.ledger.start(
            "mail.summarize",
            {"folder": "should-not-replace-first"},
            idempotency_key="request-123",
        )

        self.assertEqual(duplicate.execution_id, first.execution_id)
        self.assertEqual(duplicate.arguments, {"folder": "inbox"})
        self.assertTrue(duplicate.has_idempotency_key)
        self.assertNotIn("request-123", repr(duplicate.to_dict()))
        self.assertEqual(len(self.ledger), 1)

    def test_confirmation_resume_and_block(self):
        record = self.ledger.start("shopping.checkout", {"total": 199})
        waiting = self.ledger.request_confirmation(
            record.execution_id,
            {"message": "请在手机上确认"},
        )
        self.assertEqual(waiting.status, AWAITING_CONFIRMATION)
        self.assertIsNone(waiting.finished_at)

        resumed = self.ledger.resume(record.execution_id)
        self.assertEqual(resumed.status, RUNNING)
        self.assertIsNone(resumed.reason)

        blocked = self.ledger.block(record.execution_id, "user denied")
        self.assertEqual(blocked.status, BLOCKED)
        self.assertEqual(blocked.reason, "user denied")
        self.assertIsNotNone(blocked.finished_at)
        with self.assertRaises(InvalidExecutionTransition):
            self.ledger.complete(record.execution_id)

    def test_finish_directly_from_awaiting_confirmation(self):
        record = self.ledger.start("calendar.create")
        self.ledger.await_confirmation(record.execution_id)
        failed = self.ledger.fail(record.execution_id, "confirmation expired")
        self.assertEqual(failed.status, FAILED)

    def test_arguments_results_and_metadata_are_deep_copied(self):
        arguments = {"items": [{"name": "book"}]}
        metadata = {"device": {"name": "speaker"}}
        started = self.ledger.start("cart.add", arguments, metadata=metadata)
        arguments["items"][0]["name"] = "changed"
        metadata["device"]["name"] = "changed"
        started.arguments["items"][0]["name"] = "caller mutation"

        stored = self.ledger.get(started.execution_id)
        self.assertEqual(stored.arguments["items"][0]["name"], "book")
        self.assertEqual(stored.metadata["device"]["name"], "speaker")

        result = {"cart": [{"sku": "sku-1"}]}
        completed = self.ledger.complete(started.execution_id, result)
        result["cart"][0]["sku"] = "changed"
        completed.result["cart"][0]["sku"] = "caller mutation"
        self.assertEqual(
            self.ledger.get(started.execution_id).result["cart"][0]["sku"],
            "sku-1",
        )

    def test_recursively_redacts_credential_fields_and_obvious_values(self):
        record = self.ledger.start(
            "browser.open",
            {
                "api_key": "top-secret",
                "nested": [
                    {
                        "accessToken": "abc123",
                        "PASSWORD": "correct horse",
                        "Authorization": "Bearer secret-token",
                        "Cookie": "session=secret",
                        "safe": "Bearer abcdefghijklmnop",
                    }
                ],
            },
            metadata={"client_secret": "also-secret"},
        )
        clean = record.arguments
        self.assertEqual(clean["api_key"], REDACTED)
        self.assertEqual(clean["nested"][0]["accessToken"], REDACTED)
        self.assertEqual(clean["nested"][0]["PASSWORD"], REDACTED)
        self.assertEqual(clean["nested"][0]["Authorization"], REDACTED)
        self.assertEqual(clean["nested"][0]["Cookie"], REDACTED)
        self.assertEqual(clean["nested"][0]["safe"], REDACTED)
        self.assertEqual(record.metadata["client_secret"], REDACTED)
        self.assertNotIn("top-secret", json.dumps(record.to_dict()))

        completed = self.ledger.complete(
            record.execution_id,
            {"response": {"refresh_token": "do-not-log"}},
        )
        self.assertEqual(completed.result["response"]["refresh_token"], REDACTED)

    def test_snapshots_have_strict_size_and_shape_bounds(self):
        ledger = ExecutionLedger(
            max_snapshot_bytes=256,
            max_string_chars=32,
            max_collection_items=3,
            max_depth=2,
        )
        huge = ledger.start("search.query", {"text": "x" * 10_000})
        encoded = json.dumps(huge.arguments).encode("utf-8")
        self.assertLessEqual(len(encoded), 256)
        self.assertTrue(huge.arguments["text"].endswith("[TRUNCATED]"))

        shaped = ledger.start("list.process", [1, 2, 3, 4, 5])
        self.assertEqual(shaped.arguments[-1], {"_truncated_items": 2})

        recursive = []
        recursive.append(recursive)
        cycle = ledger.start("cycle.process", recursive)
        self.assertEqual(cycle.arguments, ["[CYCLE]"])

    def test_recent_is_newest_first_filtered_and_bounded(self):
        ledger = ExecutionLedger(max_records=3)
        active = ledger.start("weather.read", {"n": 0})
        evicted = ledger.start("weather.read", {"n": 1})
        ledger.complete(evicted.execution_id)
        second = ledger.start("calendar.read", {"n": 2})
        ledger.complete(second.execution_id)
        third = ledger.start("weather.read", {"n": 3})

        self.assertEqual(len(ledger), 3)
        self.assertEqual(len(ledger.recent()), 3)
        with self.assertRaises(ExecutionNotFoundError):
            ledger.get(evicted.execution_id)
        self.assertEqual(
            [item.execution_id for item in ledger.recent(3)],
            [third.execution_id, second.execution_id, active.execution_id],
        )
        self.assertEqual(
            [item.execution_id for item in ledger.recent(2, operation="weather.read")],
            [third.execution_id, active.execution_id],
        )
        self.assertEqual(
            ledger.recent(1, status=SUCCEEDED)[0].execution_id,
            second.execution_id,
        )

    def test_capacity_never_evicts_active_records(self):
        ledger = ExecutionLedger(max_records=2)
        first = ledger.start("tool.first")
        second = ledger.start("tool.second")

        with self.assertRaises(ExecutionCapacityError):
            ledger.start("tool.third")

        self.assertEqual(ledger.get(first.execution_id).status, RUNNING)
        self.assertEqual(ledger.get(second.execution_id).status, RUNNING)

    def test_concurrent_starts_are_unique_and_idempotent(self):
        unique_ids = []
        duplicate_ids = []
        errors = []
        output_lock = threading.Lock()

        def worker(index):
            try:
                unique = self.ledger.start("tool.run", {"index": index})
                duplicate = self.ledger.start(
                    "shared.run",
                    {"index": index},
                    idempotency_key="shared-key",
                )
                with output_lock:
                    unique_ids.append(unique.execution_id)
                    duplicate_ids.append(duplicate.execution_id)
            except Exception as exc:  # pragma: no cover - assertion reports details
                with output_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(set(unique_ids)), 24)
        self.assertEqual(len(set(duplicate_ids)), 1)
        self.assertEqual(len(self.ledger), 25)

    def test_clear_removes_records_and_idempotency_mappings(self):
        first = self.ledger.start("tool.run", idempotency_key="repeat")
        self.ledger.complete(first.execution_id)
        self.assertEqual(self.ledger.clear(), 1)
        second = self.ledger.start("tool.run", idempotency_key="repeat")
        self.assertNotEqual(first.execution_id, second.execution_id)

    def test_clear_preserves_active_records(self):
        active = self.ledger.start("tool.run")

        self.assertEqual(self.ledger.clear(), 0)
        self.assertEqual(self.ledger.get(active.execution_id).status, RUNNING)


if __name__ == "__main__":
    unittest.main()
