"""Live feed contract: bounded, sanitized, root-written events and an fd-relative session tailer."""
import base64
import importlib.util
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

CONTAINER = Path(__file__).resolve().parents[1] / "container"
JOB = "job-20261118-120000-0a1b2c3d"
POSIX_ROOT = os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def load_feed_module():
    spec = importlib.util.spec_from_file_location("re_container_livefeed", CONTAINER / "livefeed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def events(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


class FeedTestBase(unittest.TestCase):
    def setUp(self):
        self.live = load_feed_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "live"
        self.gid = 4321

    def feed(self, secrets=(b"sk-PLANTED-SECRET-VALUE",)):
        feed = self.live.LiveFeed(self.root, JOB, self.gid, secrets).open()
        self.addCleanup(feed.close)
        return feed


class LiveFeedTests(FeedTestBase):
    def test_events_are_numbered_and_carry_the_common_fields(self):
        feed = self.feed()
        feed.emit("job.claimed")
        feed.emit("agent.exited", exit_code=0, timed_out=False)
        lines = events(feed.path)
        self.assertEqual([event["seq"] for event in lines], [1, 2])
        for event in lines:
            self.assertEqual(event["v"], 1)
            self.assertEqual(event["job"], JOB)
            self.assertRegex(event["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(lines[1]["type"], "agent.exited")
        self.assertIs(lines[1]["timed_out"], False)

    @unittest.skipUnless(POSIX_ROOT, "requires Linux root for ownership checks")
    def test_feed_is_group_readable_and_root_owned(self):
        feed = self.feed()
        feed.emit("job.claimed")
        for path, mode in ((self.root, 0o750), (self.root / JOB, 0o750), (feed.path, 0o640)):
            info = os.stat(path)
            self.assertEqual(stat.S_IMODE(info.st_mode), mode, path)
            self.assertEqual((info.st_uid, info.st_gid), (0, self.gid), path)

    def test_hostile_strings_are_redacted_stripped_and_truncated(self):
        feed = self.feed()
        feed.emit("agent.text", text="key sk-PLANTED-SECRET-VALUE \x1b[2J\u202eevil\x00" + "x" * 5000)
        feed.emit("exec.request", name="r\x07.json", argv=["a" * 900] * 40,
                  timeout_s=30, memory_mb=256)
        text, request = events(feed.path)
        self.assertNotIn("PLANTED", json.dumps(text))
        self.assertIn("[REDACTED]", text["text"])
        self.assertNotIn("\x1b", text["text"])
        self.assertNotIn("\u202e", text["text"])
        self.assertLessEqual(len(text["text"]), self.live.TEXT_LIMIT)
        self.assertEqual(request["name"], "r.json")
        self.assertEqual(len(request["argv"]), self.live.ARGV_ITEMS + 1)
        self.assertTrue(all(len(item) <= self.live.ARGV_ITEM_LIMIT for item in request["argv"]))

    def test_encoded_credentials_are_redacted_in_event_payloads(self):
        token = b"fixture-???>~~~???-value"
        feed = self.feed((token,))
        values = [token.hex(), token.hex().upper()]
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            values.extend(encode(b"x" * n + token + b"y").decode("ascii") for n in range(3))
        feed.emit("job.files", entries=[{"path": value} for value in values])
        fields = events(feed.path)[0]["entries"]
        self.assertEqual(len(fields), len(values))
        for original, field in zip(values, fields):
            self.assertNotIn(original, field["path"])
            self.assertIn("[REDACTED]", field["path"])

    def test_eight_byte_credential_is_masked_in_all_base64_alignments(self):
        value = "\U0001d11e" * 2  # Eight UTF-8 bytes, only two characters.
        token = value.encode("utf-8")
        feed = self.feed((token,))
        samples = [value]
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            samples.extend(encode(b"x" * n + token + b"y").decode("ascii") for n in range(3))
        feed.emit("tool.call", argv=samples)
        masked = events(feed.path)[0]["argv"]
        self.assertEqual(len(masked), len(samples))
        self.assertTrue(all("[REDACTED]" in item for item in masked))

    def test_short_secret_values_do_not_mangle_text(self):
        feed = self.feed(secrets=(b"1", b"abc"))
        feed.emit("agent.text", text="1 + 1 = 2, abc")
        self.assertEqual(events(feed.path)[0]["text"], "1 + 1 = 2, abc")

    def test_nonfinite_numbers_never_produce_invalid_json(self):
        feed = self.feed()
        feed.emit("llm.usage", cost_usd=float("inf"), input=2 ** 70, output=3)
        event = events(feed.path)[0]
        self.assertIsNone(event["cost_usd"])
        self.assertIsNone(event["input"])
        self.assertEqual(event["output"], 3)

    def test_size_cap_truncates_once_but_keeps_the_terminal_event(self):
        with mock.patch.object(self.live, "MAX_FEED_BYTES", 2000), \
             mock.patch.object(self.live, "TERMINAL_RESERVE_BYTES", 400):
            feed = self.feed()
            for _ in range(50):
                feed.emit("agent.text", text="y" * 200)
            feed.emit("job.finished", status="done", cost_usd=0.1, cost_incomplete=False,
                      error=None)
        kinds = [event["type"] for event in events(feed.path)]
        self.assertEqual(kinds.count("feed.truncated"), 1)
        self.assertEqual(kinds[-1], "job.finished")
        self.assertLessEqual(feed.path.stat().st_size, 2000)

    def test_reopening_continues_the_sequence(self):
        first = self.feed()
        first.emit("job.claimed")
        first.close()
        second = self.feed()
        second.emit("job.verified", files=1, bytes=3)
        self.assertEqual([event["seq"] for event in events(second.path)], [1, 2])

    def test_unwritable_live_root_disables_the_feed_without_raising(self):
        self.root.parent.mkdir(exist_ok=True)
        self.root.write_text("not a directory", encoding="utf-8")
        feed = self.live.LiveFeed(self.root, JOB, self.gid).open()
        feed.emit("job.claimed")
        feed.close()

    def test_concurrent_writers_produce_whole_lines(self):
        feed = self.feed()

        def writer(tag):
            for index in range(200):
                feed.emit("agent.text", text=f"{tag}-{index}")

        threads = [threading.Thread(target=writer, args=(tag,)) for tag in "abcd"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = events(feed.path)
        self.assertEqual(len(lines), 800)
        self.assertEqual([event["seq"] for event in lines], list(range(1, 801)))

    def test_heartbeat_is_replaced_atomically(self):
        self.assertTrue(self.live.write_heartbeat(
            self.root, self.gid, state="idle", active_job=None, queue=0,
            free_bytes=1, free_inodes=2))
        self.assertTrue(self.live.write_heartbeat(
            self.root, self.gid, state="running", active_job=JOB, queue=3,
            free_bytes=1, free_inodes=2))
        body = json.loads((self.root / "dispatcher.json").read_text(encoding="utf-8"))
        self.assertEqual((body["state"], body["active_job"], body["queue"]), ("running", JOB, 3))
        self.assertEqual([p.name for p in self.root.iterdir()], ["dispatcher.json"])


class RecordMappingTests(FeedTestBase):
    def test_assistant_text_tool_calls_and_usage(self):
        record = {"type": "message", "message": {
            "role": "assistant", "model": "openai/gpt-6.1-sol", "provider": "openai",
            "content": [{"type": "text", "text": "Looking at main."},
                        {"type": "toolCall", "id": "c1", "name": "ghidra_decompile",
                         "arguments": {"address": "0x401000"}}],
            "usage": {"input": 1200, "output": 300, "cacheRead": 9000, "cacheWrite": 0,
                      "cost": {"total": 0.0123}}}}
        mapped = self.live.record_events(record)
        self.assertEqual([kind for kind, _ in mapped], ["agent.text", "tool.call", "llm.usage"])
        self.assertEqual(mapped[1][1]["tool"], "ghidra_decompile")
        self.assertIn("0x401000", mapped[1][1]["input"])
        usage = mapped[2][1]
        self.assertEqual((usage["input"], usage["output"], usage["cache_read"]), (1200, 300, 9000))
        self.assertEqual(usage["cost_usd"], 0.0123)

    def test_tool_results_and_alternative_shapes(self):
        result = {"message": {"role": "toolResult", "toolName": "bash", "toolCallId": "c1",
                              "isError": True, "content": [{"type": "text", "text": "boom"}]}}
        self.assertEqual(self.live.record_events(result), [("tool.result", {
            "tool": "bash", "call_id": "c1", "is_error": True, "output": "boom"})])
        anthropic = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "read", "input": {"path": "/job/in"}}]}
        self.assertEqual(self.live.record_events(anthropic)[0][0], "tool.call")

    def test_unknown_or_hostile_shapes_map_to_nothing(self):
        for record in (None, [], "x", {"message": []}, {"message": {"role": 7}},
                       {"message": {"role": "assistant", "usage": {"cost": {"total": "9"}},
                                    "content": [7, None, {"type": "image"}]}}):
            mapped = self.live.record_events(record)
            for kind, fields in mapped:
                self.assertEqual(kind, "llm.usage")
                self.assertEqual(fields["cost_usd"], 0.0)


class SessionTailerTests(FeedTestBase):
    def setUp(self):
        super().setUp()
        self.session = Path(self.tmp.name) / "session"
        self.session.mkdir()
        self.feed_obj = self.feed()
        self.tailer = self.live.SessionTailer(self.session, self.feed_obj)

    def texts(self):
        if not self.feed_obj.path.exists():
            return []
        return [event.get("text") for event in events(self.feed_obj.path)
                if event["type"] == "agent.text"]

    @staticmethod
    def line(text):
        return (json.dumps({"message": {"role": "assistant", "content": [
            {"type": "text", "text": text}]}}) + "\n").encode()

    def test_only_complete_lines_are_emitted_incrementally(self):
        path = self.session / "s.jsonl"
        data = self.line("one") + self.line("two")
        path.write_bytes(data[:-5])
        self.tailer.poll()
        self.assertEqual(self.texts(), ["one"])
        with path.open("ab") as handle:
            handle.write(data[-5:] + self.line("three"))
        self.tailer.poll()
        self.tailer.poll()
        self.assertEqual(self.texts(), ["one", "two", "three"])

    def test_nested_files_are_read_within_the_depth_cap(self):
        (self.session / "a" / "b" / "c").mkdir(parents=True)
        (self.session / "a" / "one.jsonl").write_bytes(self.line("depth1"))
        (self.session / "a" / "b" / "two.jsonl").write_bytes(self.line("depth2"))
        (self.session / "a" / "b" / "c" / "three.jsonl").write_bytes(self.line("depth3"))
        self.tailer.poll()
        self.assertEqual(sorted(self.texts()), ["depth1", "depth2"])

    @unittest.skipUnless(os.name == "posix", "requires POSIX links and FIFOs")
    def test_links_fifos_and_other_files_are_ignored_without_blocking(self):
        outside = Path(self.tmp.name) / "outside.jsonl"
        outside.write_bytes(self.line("outside"))
        os.symlink(outside, self.session / "link.jsonl")
        os.symlink(self.tmp.name, self.session / "dirlink")
        os.mkfifo(self.session / "pipe.jsonl")
        (self.session / "notes.txt").write_bytes(self.line("txt"))
        worker = threading.Thread(target=self.tailer.poll, daemon=True)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "a FIFO blocked the tailer")
        self.assertEqual(self.texts(), [])

    def test_oversized_and_malformed_lines_are_skipped(self):
        with mock.patch.object(self.live.SessionTailer, "MAX_LINE", 200):
            tailer = self.live.SessionTailer(self.session, self.feed_obj)
            (self.session / "s.jsonl").write_bytes(
                b"{not json}\n" + b"[" * 50 + b"\n" + b"z" * 1000 + b"\n" + self.line("after"))
            for _ in range(5):
                tailer.poll()
        self.assertEqual(self.texts(), ["after"])

    def test_rewritten_files_restart_from_the_beginning(self):
        path = self.session / "s.jsonl"
        path.write_bytes(self.line("first") + self.line("second"))
        self.tailer.poll()
        path.write_bytes(self.line("new"))
        self.tailer.poll()
        self.assertEqual(self.texts(), ["first", "second", "new"])

    def test_read_budget_spreads_large_backlogs_over_polls(self):
        with mock.patch.object(self.live.SessionTailer, "MAX_EVENTS", 3):
            tailer = self.live.SessionTailer(self.session, self.feed_obj)
            (self.session / "s.jsonl").write_bytes(b"".join(self.line(str(n)) for n in range(7)))
            tailer.poll()
            self.assertEqual(self.texts(), ["0", "1", "2"])
            tailer.poll()
            tailer.poll()
        self.assertEqual(self.texts(), [str(n) for n in range(7)])

    def test_missing_session_directory_is_quiet(self):
        tailer = self.live.SessionTailer(self.session / "missing", self.feed_obj)
        tailer.poll()
        self.assertEqual(self.texts(), [])


if __name__ == "__main__":
    unittest.main()
