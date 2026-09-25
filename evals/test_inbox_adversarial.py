#!/usr/bin/env python3
"""Adversarial stress-testing suite for scripts/inbox.py.

Empirically tests concurrency, atomicity, race conditions, and edge cases under
extreme load and contention:
1. 20 concurrent worker processes sending 1,000 total messages.
2. Rapid concurrent draining (tight loop) during high-throughput writing.
3. Multiple competing concurrent drainers contending simultaneously on the same inbox.
4. Concurrent read_message(ack=True) idempotence and race behavior.
5. Corrupted file injection during high concurrent write/drain activity.
"""

from __future__ import annotations

from datetime import datetime
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
import uuid

# Setup sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import inbox


def _writer_worker(
    worker_id: int,
    num_messages: int,
    recipient: str,
    mailbox_dir: str,
    barrier: Any = None,
    delay_ms: float = 0.0,
) -> None:
    """Worker process for sending messages concurrently."""
    if barrier is not None:
        try:
            barrier.wait(timeout=15)
        except Exception:
            pass

    for i in range(num_messages):
        inbox.send_message(
            recipient=recipient,
            sender=f"worker-{worker_id}",
            body=f"Payload {i} from worker {worker_id}",
            msg_type="progress",
            payload={"worker_id": worker_id, "seq": i, "token": uuid.uuid4().hex},
            mailbox_dir=mailbox_dir,
        )
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)


def _competing_drainer_worker(
    drainer_id: int,
    recipient: str,
    mailbox_dir: str,
    barrier: Any,
    output_queue: Any,
) -> None:
    """Drainer worker process for competing drainer test."""
    if barrier is not None:
        try:
            barrier.wait(timeout=10)
        except Exception:
            pass

    try:
        messages = inbox.drain_inbox(recipient, mailbox_dir=mailbox_dir)
        # Put message IDs back in queue
        msg_ids = [m.message_id for m in messages]
        output_queue.put((drainer_id, msg_ids, None))
    except Exception as exc:
        output_queue.put((drainer_id, [], str(exc)))


class AdversarialInboxStressTest(unittest.TestCase):
    """Adversarial stress-test cases targeting concurrency, race conditions, and data integrity."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.mailbox_dir = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_adv_01_20_workers_1000_messages(self) -> None:
        """Adversarial 1: 20 concurrent worker processes sending 50 msgs each (1,000 total)."""
        num_workers = 20
        msgs_per_worker = 50
        total_expected = num_workers * msgs_per_worker
        recipient = "coord_adv_1"

        mp_ctx = multiprocessing.get_context("spawn")
        barrier = mp_ctx.Barrier(num_workers)

        start_time = time.time()
        processes = []
        for wid in range(num_workers):
            p = mp_ctx.Process(
                target=_writer_worker,
                args=(wid, msgs_per_worker, recipient, str(self.mailbox_dir), barrier, 0.0),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=30)
            self.assertFalse(p.is_alive(), f"Worker {p.pid} hung or deadlocked")
            self.assertEqual(p.exitcode, 0, f"Worker {p.pid} exited with code {p.exitcode}")

        elapsed = time.time() - start_time
        print(f"\n[Adv-1] 20 workers sent {total_expected} messages in {elapsed:.2f}s ({total_expected/elapsed:.1f} msg/s)")

        # Verify incoming folder contents
        incoming = self.mailbox_dir / recipient / "incoming"
        files = list(incoming.glob("*.toon"))
        self.assertEqual(len(files), total_expected, f"Expected {total_expected} files, found {len(files)}")

        # Verify all messages can be listed and parsed
        messages = inbox.list_messages(recipient, unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(messages), total_expected)

        # Check uniqueness and monotonic sequence
        seen_ids = set()
        for wid in range(num_workers):
            w_msgs = [m for m in messages if m.payload.get("worker_id") == wid]
            self.assertEqual(len(w_msgs), msgs_per_worker)
            seqs = [m.payload["seq"] for m in w_msgs]
            self.assertEqual(sorted(seqs), list(range(msgs_per_worker)))
            for m in w_msgs:
                self.assertNotIn(m.message_id, seen_ids)
                seen_ids.add(m.message_id)

    def test_adv_02_rapid_drain_while_writing(self) -> None:
        """Adversarial 2: 10 worker processes writing 100 messages each (1,000 total) while drainer loops with zero sleep."""
        num_workers = 10
        msgs_per_worker = 100
        total_expected = num_workers * msgs_per_worker
        recipient = "coord_adv_2"

        mp_ctx = multiprocessing.get_context("spawn")
        barrier = mp_ctx.Barrier(num_workers)

        drained_messages: list[Any] = []
        drained_lock = threading.Lock()
        stop_drainer = threading.Event()
        drainer_exceptions: list[Exception] = []

        def tight_drainer() -> None:
            while not stop_drainer.is_set():
                try:
                    batch = inbox.drain_inbox(recipient, mailbox_dir=self.mailbox_dir)
                    if batch:
                        with drained_lock:
                            drained_messages.extend(batch)
                except Exception as exc:
                    drainer_exceptions.append(exc)
                # No sleep or tiny yield to maximize interleaving
                time.sleep(0.005)

        drainer_thread = threading.Thread(target=tight_drainer)
        drainer_thread.start()

        start_time = time.time()
        processes = []
        for wid in range(num_workers):
            p = mp_ctx.Process(
                target=_writer_worker,
                args=(wid, msgs_per_worker, recipient, str(self.mailbox_dir), barrier, 1.0),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=35)
            self.assertFalse(p.is_alive(), f"Worker {p.pid} hung")
            self.assertEqual(p.exitcode, 0)

        # Stop drainer and final drain pass
        stop_drainer.set()
        drainer_thread.join(timeout=5)

        final_batch = inbox.drain_inbox(recipient, mailbox_dir=self.mailbox_dir)
        with drained_lock:
            drained_messages.extend(final_batch)

        elapsed = time.time() - start_time
        print(f"\n[Adv-2] Rapid drain during 1000 writes in {elapsed:.2f}s")
        print(f"Total drained: {len(drained_messages)}, Drainer exceptions: {len(drainer_exceptions)}")

        self.assertEqual(drainer_exceptions, [])
        self.assertEqual(len(drained_messages), total_expected)

        # Verify no duplicate message IDs
        ids = [m.message_id for m in drained_messages]
        self.assertEqual(len(set(ids)), total_expected)

        # Verify all sequence IDs accounted for
        for wid in range(num_workers):
            w_msgs = [m for m in drained_messages if m.payload.get("worker_id") == wid]
            self.assertEqual(len(w_msgs), msgs_per_worker)
            seqs = [m.payload["seq"] for m in w_msgs]
            self.assertEqual(sorted(seqs), list(range(msgs_per_worker)))

    def test_adv_03_competing_concurrent_drainers(self) -> None:
        """Adversarial 3: Multiple concurrent drainer processes contending on the same inbox."""
        recipient = "coord_adv_3"
        total_msgs = 300
        num_drainers = 6

        # Pre-populate 300 messages
        for i in range(total_msgs):
            inbox.send_message(
                recipient=recipient,
                sender="loader",
                body=f"Preloaded message {i}",
                payload={"index": i},
                mailbox_dir=self.mailbox_dir,
            )

        mp_ctx = multiprocessing.get_context("spawn")
        barrier = mp_ctx.Barrier(num_drainers)
        queue = mp_ctx.Queue()

        processes = []
        for did in range(num_drainers):
            p = mp_ctx.Process(
                target=_competing_drainer_worker,
                args=(did, recipient, str(self.mailbox_dir), barrier, queue),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=20)
            self.assertFalse(p.is_alive())
            self.assertEqual(p.exitcode, 0)

        results = []
        while not queue.empty():
            results.append(queue.get())

        self.assertEqual(len(results), num_drainers)

        all_drained_ids = []
        drainer_counts = {}
        for did, msg_ids, err in results:
            self.assertIsNone(err, f"Drainer {did} raised error: {err}")
            drainer_counts[did] = len(msg_ids)
            all_drained_ids.extend(msg_ids)

        # Also check if any messages remained undrained
        remaining = inbox.drain_inbox(recipient, mailbox_dir=self.mailbox_dir)
        all_drained_ids.extend([m.message_id for m in remaining])

        print(f"\n[Adv-3] Competing drainer distribution: {drainer_counts}, Remaining: {len(remaining)}")
        print(f"Total messages drained across all drainers: {len(all_drained_ids)} (expected {total_msgs})")

        # Invariant 1: Total messages drained must equal total messages sent
        self.assertEqual(len(all_drained_ids), total_msgs)

        # Invariant 2: Mutual exclusivity - no duplicate message IDs across drainers
        self.assertEqual(len(set(all_drained_ids)), total_msgs, "Duplicate reads detected between competing drainers!")

    def test_adv_04_concurrent_read_ack_race(self) -> None:
        """Adversarial 4: Concurrent read_message(ack=True) calls racing on the same unread message."""
        recipient = "coord_adv_4"
        rounds = 30
        concurrency = 10

        corrupted_errors = []
        not_found_errors = []
        successes = []

        for r in range(rounds):
            msg = inbox.send_message(
                recipient=recipient,
                sender="sender",
                body=f"Race test round {r}",
                payload={"round": r},
                mailbox_dir=self.mailbox_dir,
            )
            mid = msg.message_id

            barrier = threading.Barrier(concurrency)

            def reader_task() -> None:
                barrier.wait()
                try:
                    res = inbox.read_message(recipient, mid, ack=True, mailbox_dir=self.mailbox_dir)
                    successes.append(res)
                except inbox.CorruptedMessageError as cme:
                    corrupted_errors.append(cme)
                except inbox.MessageNotFoundError as mnfe:
                    not_found_errors.append(mnfe)

            threads = [threading.Thread(target=reader_task) for _ in range(concurrency)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        print(f"\n[Adv-4] Concurrent read_ack race over {rounds} rounds ({rounds * concurrency} attempts):")
        print(f"Successes: {len(successes)}")
        print(f"CorruptedMessageError: {len(corrupted_errors)}")
        print(f"MessageNotFoundError: {len(not_found_errors)}")

        # Check if false CorruptedMessageError was raised
        self.assertEqual(
            len(corrupted_errors),
            0,
            f"Race condition bug: read_message falsely raised CorruptedMessageError {len(corrupted_errors)} times on valid messages due to concurrent file move!",
        )

    def test_adv_05_corrupted_file_injection_during_heavy_traffic(self) -> None:
        """Adversarial 5: Corrupted files injected into incoming/ during concurrent write and drain."""
        recipient = "coord_adv_5"
        num_workers = 5
        msgs_per_worker = 50
        total_valid = num_workers * msgs_per_worker

        mp_ctx = multiprocessing.get_context("spawn")
        barrier = mp_ctx.Barrier(num_workers)

        drained_messages: list[Any] = []
        drained_lock = threading.Lock()
        stop_event = threading.Event()

        def drainer() -> None:
            while not stop_event.is_set():
                try:
                    b = inbox.drain_inbox(recipient, mailbox_dir=self.mailbox_dir)
                    if b:
                        with drained_lock:
                            drained_messages.extend(b)
                except Exception:
                    pass
                time.sleep(0.01)

        d_thread = threading.Thread(target=drainer)
        d_thread.start()

        processes = []
        for wid in range(num_workers):
            p = mp_ctx.Process(
                target=_writer_worker,
                args=(wid, msgs_per_worker, recipient, str(self.mailbox_dir), barrier, 1.0),
            )
            processes.append(p)
            p.start()

        # Inject various corrupted files while workers write
        incoming_dir = self.mailbox_dir / recipient / "incoming"
        incoming_dir.mkdir(parents=True, exist_ok=True)
        time.sleep(0.05)

        injected = [
            ("bad_json_1.json", '{"message_id": "incomplete"'),
            ("bad_json_2.json", "{not even json}"),
            ("zero_byte_1.json", ""),
            ("missing_fields_1.json", '{"message_id": "bad1"}'),
        ]
        for fname, content in injected:
            try:
                (incoming_dir / fname).write_text(content, encoding="utf-8")
            except Exception:
                pass
            time.sleep(0.02)

        for p in processes:
            p.join(timeout=20)
            self.assertEqual(p.exitcode, 0)

        stop_event.set()
        d_thread.join(timeout=5)

        # Final drain
        final_b = inbox.drain_inbox(recipient, mailbox_dir=self.mailbox_dir)
        with drained_lock:
            drained_messages.extend(final_b)

        print(f"\n[Adv-5] Drained valid messages: {len(drained_messages)} (expected {total_valid})")

        # Invariant 1: All valid messages collected
        self.assertEqual(len(drained_messages), total_valid)

        # Invariant 2: Corrupted files are in dead-letter, none in incoming
        dead_letter_dir = self.mailbox_dir / recipient / "dead-letter"
        dead_files = list(dead_letter_dir.glob("*.json"))
        print(f"Quarantined in dead-letter: {len(dead_files)}")
        self.assertGreaterEqual(len(dead_files), len(injected))
        self.assertEqual(len(list(incoming_dir.glob("*.json"))), 0)


if __name__ == "__main__":
    unittest.main()
