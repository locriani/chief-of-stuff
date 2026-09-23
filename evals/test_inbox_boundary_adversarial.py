"""Adversarial boundary, fault-injection, security, and CLI robustness test suite.

Author: teamwork_preview_challenger_2
Target: scripts/inbox.py
Coverage:
- Task 1: Malformed and corrupted file handling (quarantine to dead-letter, sibling delivery)
- Task 2: Path traversal and security probes (recipients, message_ids, senders)
- Task 3: CLI robustness (flags, subcommands, exit codes 0/1/2, >500KB payload, Unicode, empty bodies)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import inbox
from inbox import (
    CorruptedMessageError,
    InboxError,
    InvalidMessageError,
    Message,
    MessageNotFoundError,
)

INBOX_SCRIPT = SCRIPTS_DIR / "inbox.py"


class BaseAdversarialTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mailbox_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(
        self,
        args: list[str],
        expected_code: int | None = 0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(INBOX_SCRIPT)] + args
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if expected_code is not None:
            self.assertEqual(
                proc.returncode,
                expected_code,
                f"CLI returned {proc.returncode} (expected {expected_code}).\n"
                f"Cmd: {' '.join(cmd)}\n"
                f"Stdout:\n{proc.stdout}\n"
                f"Stderr:\n{proc.stderr}",
            )
        return proc


class Task1MalformedAndCorruptedFileTest(BaseAdversarialTestCase):
    """Task 1: Fault injection of corrupted files into incoming/."""

    def test_truncated_json_quarantined_valid_sibling_delivered(self) -> None:
        valid_msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body="Valid sibling 1",
            mailbox_dir=self.mailbox_dir,
        )

        incoming = self.mailbox_dir / "coordinator" / "incoming"
        corrupt_path = incoming / "msg_truncated.json"
        corrupt_path.write_text('{"message_id": "corrupted1", "body": "unfinishe')

        # Verify list_messages quarantines corrupt and returns valid
        messages = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, valid_msg.message_id)

        # Verify file quarantined to dead-letter/
        self.assertFalse(corrupt_path.exists())
        dead_letter = self.mailbox_dir / "coordinator" / "dead-letter"
        self.assertTrue((dead_letter / "msg_truncated.json").exists())

    def test_binary_garbage_quarantined_valid_sibling_delivered(self) -> None:
        valid_msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body="Valid sibling 2",
            mailbox_dir=self.mailbox_dir,
        )

        incoming = self.mailbox_dir / "coordinator" / "incoming"
        bin_path = incoming / "garbage.bin"
        bin_path.write_bytes(b"\x00\xff\xfe\x12\x80\x99\xff\xaa\xbb\xcc\xdd\xee\x81\x82\x83")

        messages = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, valid_msg.message_id)

        self.assertFalse(bin_path.exists())
        dead_letter = self.mailbox_dir / "coordinator" / "dead-letter"
        self.assertTrue((dead_letter / "garbage.bin").exists())

    def test_zero_byte_file_quarantined_valid_sibling_delivered(self) -> None:
        valid_msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body="Valid sibling 3",
            mailbox_dir=self.mailbox_dir,
        )

        incoming = self.mailbox_dir / "coordinator" / "incoming"
        zero_path = incoming / "zero_byte.json"
        zero_path.write_bytes(b"")

        drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].message_id, valid_msg.message_id)

        self.assertFalse(zero_path.exists())
        dead_letter = self.mailbox_dir / "coordinator" / "dead-letter"
        self.assertTrue((dead_letter / "zero_byte.json").exists())

    def test_missing_schema_keys_quarantined(self) -> None:
        required_keys = ["message_id", "timestamp", "sender", "recipient", "type"]
        incoming = self.mailbox_dir / "coordinator" / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)

        for key in required_keys:
            data = {
                "message_id": f"msg_missing_{key}",
                "timestamp": "2026-09-22T04:00:00.000000Z",
                "sender": "worker",
                "recipient": "coordinator",
                "type": "generic",
                "body": "test",
                "payload": {},
            }
            del data[key]
            (incoming / f"missing_{key}.json").write_text(json.dumps(data))

        # Add valid sibling
        valid_msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-valid",
            body="Valid sibling 4",
            mailbox_dir=self.mailbox_dir,
        )

        messages = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, valid_msg.message_id)

        dead_letter = self.mailbox_dir / "coordinator" / "dead-letter"
        for key in required_keys:
            self.assertFalse((incoming / f"missing_{key}.json").exists())
            self.assertTrue((dead_letter / f"missing_{key}.json").exists())

    def test_non_dict_json_roots_quarantined(self) -> None:
        incoming = self.mailbox_dir / "coordinator" / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)

        bad_roots = {
            "root_array.json": "[1, 2, 3]",
            "root_int.json": "12345",
            "root_string.json": '"a string"',
            "root_null.json": "null",
            "root_bool.json": "false",
        }
        for fname, content in bad_roots.items():
            (incoming / fname).write_text(content)

        valid_msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-valid",
            body="Valid sibling 5",
            mailbox_dir=self.mailbox_dir,
        )

        messages = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, valid_msg.message_id)

        dead_letter = self.mailbox_dir / "coordinator" / "dead-letter"
        for fname in bad_roots:
            self.assertFalse((incoming / fname).exists())
            self.assertTrue((dead_letter / fname).exists())

    def test_read_message_on_corrupted_quarantines_and_raises(self) -> None:
        incoming = self.mailbox_dir / "coordinator" / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        corrupt_target = incoming / "msg_direct_corrupt.json"
        corrupt_target.write_text("{malformed json")

        with self.assertRaises(CorruptedMessageError):
            inbox.read_message(
                recipient="coordinator",
                message_id="msg_direct_corrupt",
                mailbox_dir=self.mailbox_dir,
            )

        self.assertFalse(corrupt_target.exists())
        dead_letter = self.mailbox_dir / "coordinator" / "dead-letter"
        self.assertTrue((dead_letter / "msg_direct_corrupt.json").exists())


class Task2PathTraversalAndSecurityTest(BaseAdversarialTestCase):
    """Task 2: Path traversal, null byte, metacharacter, and security boundaries."""

    def test_recipient_path_traversal_rejection(self) -> None:
        traversal_attempts = [
            "../escape",
            "../../etc/passwd",
            "../../../outside",
            "..\\windows\\win.ini",
            "/absolute/path",
            "/etc/passwd",
            "sub/dir/recipient",
            "escaped/../../secret",
            "null\x00byte",
            "user\x00/root",
        ]
        for bad_recip in traversal_attempts:
            with self.assertRaises(InvalidMessageError, msg=f"Should reject recipient {bad_recip!r}"):
                inbox.send_message(
                    recipient=bad_recip,
                    sender="worker-1",
                    body="exploit",
                    mailbox_dir=self.mailbox_dir,
                )

            with self.assertRaises(InvalidMessageError, msg=f"Should reject list on {bad_recip!r}"):
                inbox.list_messages(recipient=bad_recip, mailbox_dir=self.mailbox_dir)

            with self.assertRaises(InvalidMessageError, msg=f"Should reject drain on {bad_recip!r}"):
                inbox.drain_inbox(recipient=bad_recip, mailbox_dir=self.mailbox_dir)

        # Confirm no directories were created targeting escape or outside
        self.assertFalse((self.mailbox_dir.parent / "escape").exists())
        self.assertFalse((self.mailbox_dir.parent / "outside").exists())

    def test_recipient_dot_and_boundary_rejection(self) -> None:
        invalid_recipients = [
            "",
            "   ",
            "\t",
            "\n",
            ".",
            "..",
            "...",
            "____",
            "_____",
            "....",
        ]
        for bad in invalid_recipients:
            with self.assertRaises(InvalidMessageError, msg=f"Should reject: {bad!r}"):
                inbox.sanitize_recipient(bad)

    def test_recipient_metacharacter_sanitization(self) -> None:
        metachar_tests = [
            ("worker*1", "worker_1"),
            ("worker?1", "worker_1"),
            ("worker|pipe", "worker_pipe"),
            ("worker<test>", "worker_test_"),
            ("worker;cmd", "worker_cmd"),
            ("worker$env", "worker_env"),
            ("worker`echo`", "worker_echo_"),
            ("worker\nnewline", "worker_newline"),
            ("worker\ttab", "worker_tab"),
        ]
        for raw, expected in metachar_tests:
            sanitized = inbox.sanitize_recipient(raw)
            self.assertEqual(sanitized, expected)
            msg = inbox.send_message(
                recipient=raw,
                sender="worker-sender",
                body="test body",
                mailbox_dir=self.mailbox_dir,
            )
            self.assertEqual(msg.recipient, expected)
            self.assertTrue((self.mailbox_dir / expected).is_dir())

    def test_sender_validation_probes(self) -> None:
        invalid_senders = ["", "   ", "\t", "\n"]
        for bad_sender in invalid_senders:
            with self.assertRaises(InvalidMessageError):
                inbox.validate_sender(bad_sender)
            with self.assertRaises(InvalidMessageError):
                inbox.send_message(
                    recipient="coordinator",
                    sender=bad_sender,
                    body="body",
                    mailbox_dir=self.mailbox_dir,
                )

    def test_message_id_path_traversal_rejection(self) -> None:
        malicious_message_ids = [
            "../outside",
            "../../etc/passwd",
            "..\\windows\\system32",
            "/absolute/root",
            "sub/msg",
            "msg\x00injection",
            "*.json",
            "",
            "   ",
            ".hidden",
            "-leading-dash",
            "$variable",
            "msg;rm -rf",
        ]
        for bad_id in malicious_message_ids:
            with self.assertRaises(InvalidMessageError, msg=f"Should reject message_id {bad_id!r}"):
                inbox.validate_message_id(bad_id)

            with self.assertRaises(InvalidMessageError, msg=f"Should reject in read: {bad_id!r}"):
                inbox.read_message("coordinator", bad_id, mailbox_dir=self.mailbox_dir)

            with self.assertRaises(InvalidMessageError, msg=f"Should reject in ack: {bad_id!r}"):
                inbox.ack_message("coordinator", bad_id, mailbox_dir=self.mailbox_dir)


class Task3CLIRobustnessTest(BaseAdversarialTestCase):
    """Task 3: CLI syntax, missing flags, invalid subcommands, exit codes, large payload, Unicode."""

    def test_cli_missing_subcommand_exit_code_2(self) -> None:
        proc = self.run_cli([], expected_code=2)
        self.assertIn("usage", proc.stderr.lower())

    def test_cli_invalid_subcommand_exit_code_2(self) -> None:
        proc = self.run_cli(["nonexistent_subcommand"], expected_code=2)
        self.assertIn("invalid choice", proc.stderr.lower())

    def test_cli_missing_required_flags_exit_code_2(self) -> None:
        self.run_cli(["send", "--to", "coordinator", "--body", "test"], expected_code=2)
        self.run_cli(["send", "--from", "worker", "--body", "test"], expected_code=2)
        self.run_cli(["list"], expected_code=2)
        self.run_cli(["read", "--recipient", "coordinator"], expected_code=2)
        self.run_cli(["read", "msg_123"], expected_code=2)
        self.run_cli(["ack", "--recipient", "coordinator"], expected_code=2)
        self.run_cli(["ack", "msg_123"], expected_code=2)
        self.run_cli(["drain"], expected_code=2)
        self.run_cli(["status"], expected_code=2)

    def test_cli_invalid_arguments_exit_code_2(self) -> None:
        # Invalid type
        self.run_cli(
            [
                "send",
                "--to", "coordinator",
                "--from", "worker",
                "--type", "bogus_type",
                "--body", "test",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=2,
        )
        # Invalid payload JSON
        self.run_cli(
            [
                "send",
                "--to", "coordinator",
                "--from", "worker",
                "--body", "test",
                "--payload", "{invalid: json",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=2,
        )
        # Missing payload file
        self.run_cli(
            [
                "send",
                "--to", "coordinator",
                "--from", "worker",
                "--body", "test",
                "--payload-file", "/nonexistent/payload.json",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=2,
        )
        # Path traversal in recipient
        self.run_cli(
            [
                "send",
                "--to", "../escaped",
                "--from", "worker",
                "--body", "test",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=2,
        )
        # Path traversal in message_id
        self.run_cli(
            [
                "read",
                "../escaped_id",
                "--recipient", "coordinator",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=2,
        )

    def test_cli_domain_errors_exit_code_1(self) -> None:
        # Nonexistent message read
        proc_read = self.run_cli(
            [
                "read",
                "msg_20260922T000000000000Z_abcdef123456",
                "--recipient", "coordinator",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=1,
        )
        self.assertIn("not found", proc_read.stderr.lower())

        # Nonexistent message ack
        proc_ack = self.run_cli(
            [
                "ack",
                "msg_20260922T000000000000Z_abcdef123456",
                "--recipient", "coordinator",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=1,
        )
        self.assertIn("not found", proc_ack.stderr.lower())

    def test_cli_empty_body_rules(self) -> None:
        # Empty body without payload -> code 2
        self.run_cli(
            [
                "send",
                "--to", "coordinator",
                "--from", "worker",
                "--body", "",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=2,
        )
        # Empty body with payload -> code 0
        proc = self.run_cli(
            [
                "send",
                "--to", "coordinator",
                "--from", "worker",
                "--body", "",
                "--payload", '{"command": "ping"}',
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=0,
        )
        data = json.loads(proc.stdout)
        self.assertEqual(data["payload"], {"command": "ping"})
        # When body is omitted, payload is serialized to body for text display
        self.assertEqual(data["body"], '{"command": "ping"}')

        # Empty body with explicit payload body key -> code 0
        proc2 = self.run_cli(
            [
                "send",
                "--to", "coordinator",
                "--from", "worker",
                "--body", "",
                "--payload", '{"body": "", "command": "ping"}',
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=0,
        )
        data2 = json.loads(proc2.stdout)
        self.assertEqual(data2["body"], "")
        self.assertEqual(data2["payload"], {"body": "", "command": "ping"})

    def test_cli_large_body_over_500kb(self) -> None:
        large_content = "AdversarialPayloadTestLine_500KB\n" * 18_000  # ~594 KB
        self.assertGreater(len(large_content), 500_000)

        # 1. Direct CLI body
        proc_send = self.run_cli(
            [
                "send",
                "--to", "coordinator",
                "--from", "worker-heavy",
                "--body", large_content,
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=0,
        )
        msg_id = json.loads(proc_send.stdout)["message_id"]

        proc_read = self.run_cli(
            [
                "read",
                msg_id,
                "--recipient", "coordinator",
                "--format", "json",
                "--no-ack",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=0,
        )
        read_data = json.loads(proc_read.stdout)
        self.assertEqual(read_data["body"], large_content)

        # 2. Large payload file (1MB+)
        payload_file = self.mailbox_dir / "large_payload.json"
        huge_dict = {f"k_{i}": f"v_{i}" * 50 for i in range(2500)}
        payload_file.write_text(json.dumps(huge_dict))
        self.assertGreater(payload_file.stat().st_size, 500_000)

        proc_payload_send = self.run_cli(
            [
                "send",
                "--to", "coordinator",
                "--from", "worker-payload",
                "--body", "large payload file test",
                "--payload-file", str(payload_file),
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=0,
        )
        file_msg_id = json.loads(proc_payload_send.stdout)["message_id"]

        proc_payload_read = self.run_cli(
            [
                "read",
                file_msg_id,
                "--recipient", "coordinator",
                "--format", "json",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=0,
        )
        self.assertEqual(json.loads(proc_payload_read.stdout)["payload"], huge_dict)

    def test_cli_unicode_multilingual_and_emojis(self) -> None:
        unicode_samples = [
            "Emojis: 👨‍👩‍👧‍👦 🏳️‍🌈 🚀 📬 💥 🔥 ✨",
            "CJK: 繁體中文 简体中文 日本語 かな カナ 한국어",
            "RTL: العربية (Arabic) עברית (Hebrew)",
            "Indic: हिन्दी (Devanagari) ภาษาไทย (Thai)",
            "Symbols: ä ö ü é à ç ñ ∑ ∫ ∂ √ π ≠ ≤ ≥",
        ]
        for i, text in enumerate(unicode_samples):
            self.run_cli(
                [
                    "send",
                    "--to", "coordinator",
                    "--from", f"worker-{i}",
                    "--body", text,
                    "--mailbox-dir", str(self.mailbox_dir),
                ],
                expected_code=0,
            )

        # Drain via CLI and verify all Unicode content
        proc_drain = self.run_cli(
            [
                "drain",
                "--recipient", "coordinator",
                "--format", "json",
                "--mailbox-dir", str(self.mailbox_dir),
            ],
            expected_code=0,
        )
        drained = json.loads(proc_drain.stdout)
        self.assertEqual(len(drained), len(unicode_samples))
        drained_bodies = [m["body"] for m in drained]
        for text in unicode_samples:
            self.assertIn(text, drained_bodies)


if __name__ == "__main__":
    unittest.main()
