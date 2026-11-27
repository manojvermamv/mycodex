"""Consolidated regressions for the codex 0.169.0 code audit (#1-#20, M1-M5)."""

import contextlib
import email.message
import fcntl
import io
import json
import sqlite3
import subprocess
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from test_core import POSIX, Sandbox, write_auth
from mycodex import (
    accounts, appserver, auth, auto_resume, codex, config, launch,
    maintenance, model_cache, paths, procs, profiles, proxy,
    redemption, remote, remote_cmd, state,
)


class _FakeHTTPResp:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


# ====================================================================== Storage, Cache, Locks (#1, M1, #2, #3, #7, #18, M3)
class FoundationAndStorageTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.close()

    def test_1_and_m1_model_cache_envelope_refresh_and_sync_validation(self):
        src = profiles.Profile("plus", self.box.home("plus"))
        dst = profiles.Profile("relay", self.box.home("relay"))
        write_auth(src.home, "acct-p", "p@example.com")
        write_auth(dst.home, "acct-r", "r@example.com")
        (src.home / "models_cache.json").write_text(json.dumps({
            "fetched_at": "2026-11-20T00:00:00Z", "etag": "old",
            "client_version": "0.168.0", "identity": "plus-id", "models": [],
        }))

        seen = []
        def fake_urlopen(req, timeout=10):
            seen.append((req.full_url, {k.lower(): v for k, v in req.header_items()}))
            if len(seen) == 1:
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", email.message.Message(), io.BytesIO(b""))
            return _FakeHTTPResp(json.dumps({"models": [{"slug": "gpt-6.2"}]}).encode(), headers={"ETag": 'W/"fresh"'})

        with mock.patch.object(codex, "version", return_value="0.170.1"), \
             mock.patch.object(auth, "credentials", return_value=auth.Credentials("stale", "acct-p", None)), \
             mock.patch.object(auth, "refresh", return_value=auth.Credentials("fresh", "acct-p", None)), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            model_cache._fetch_envelope(src)

        self.assertIn("client_version=0.170.1", seen[0][0])
        self.assertEqual(seen[0][1].get("chatgpt-account-id"), "acct-p")
        self.assertEqual(seen[1][1].get("authorization"), "Bearer fresh")
        written_src = json.loads((src.home / "models_cache.json").read_text())
        self.assertEqual(written_src["identity"], "plus-id")
        self.assertEqual(written_src["etag"], 'W/"fresh"')

        # M1: valid relay cache rejects invalid/date-only/integer-etag/envelope-less sources
        valid_dst = {
            "fetched_at": "2026-11-26T00:00:00Z", "etag": "e1",
            "client_version": "0.169.0", "identity": "relay-id", "models": [{"slug": "keep"}],
        }
        (dst.home / "models_cache.json").write_text(json.dumps(valid_dst))
        for bad_src in (
            {"models": []},
            {"fetched_at": "2026-11-26", "etag": "e2", "client_version": "0.169.0", "models": []},
            {"fetched_at": "2026-11-26T00:00:00Z", "etag": 123, "client_version": "0.169.0", "models": []},
        ):
            (src.home / "models_cache.json").write_text(json.dumps(bad_src))
            with self.assertRaises(model_cache.ModelCacheError):
                model_cache.sync(src, dst)
        self.assertEqual(json.loads((dst.home / "models_cache.json").read_text()), valid_dst)

        # #1: syncing valid source into fresh relay never inherits source identity
        (src.home / "models_cache.json").write_text(json.dumps(written_src))
        dst2 = profiles.Profile("relay2", self.box.home("relay2"))
        write_auth(dst2.home, "acct-r2", "r2@example.com")
        model_cache.sync(src, dst2)
        self.assertIsNone(json.loads((dst2.home / "models_cache.json").read_text()).get("identity"))

    def test_2_18_m3_prepare_home_private_db_sidecars_and_codex_0169_locks(self):
        home = self.box.home("p")
        home.mkdir(parents=True)
        # Private DB absent from shared home still unlinks -wal/-shm symlinks
        (home / "logs_99.sqlite").write_text("private")
        (home / "logs_99.sqlite-wal").symlink_to(self.box.shared / "logs_99.sqlite-wal")
        (home / "logs_99.sqlite-shm").symlink_to(self.box.shared / "logs_99.sqlite-shm")
        for rel in (".sqlite-maintenance.lock", ".tmp/plugins.sync.lock", "session_index.jsonl.lock"):
            p = home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")

        preview = profiles.prepare_home(home, adopt=True, dry_run=True)
        self.assertTrue(any("logs_99.sqlite-wal" in s for s in preview.linked))
        self.assertTrue((home / "logs_99.sqlite-wal").is_symlink())

        repaired = profiles.prepare_home(home, adopt=True, dry_run=False)
        self.assertFalse((home / "logs_99.sqlite-wal").is_symlink())
        self.assertFalse((home / "logs_99.sqlite-shm").is_symlink())
        for rel in (".sqlite-maintenance.lock", ".tmp/plugins.sync.lock", "session_index.jsonl.lock"):
            self.assertIn(rel, repaired.adopted)
            self.assertTrue((home / rel).is_symlink())

    def test_3_state_cache_isolation_and_external_invalidation(self):
        state.block("acct", time.time() + 100, "old-reason")
        entered = threading.Event()
        proceed = threading.Event()
        orig_read = Path.read_text

        def slow_read(path_self, *args, **kwargs):
            txt = orig_read(path_self, *args, **kwargs)
            if path_self == paths.ACCOUNTS_STATE and not entered.is_set():
                entered.set()
                proceed.wait(timeout=2)
            return txt

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=slow_read):
            t = threading.Thread(target=state.load)
            t.start()
            entered.wait(timeout=2)
            paths.ACCOUNTS_STATE.write_text(json.dumps({"profiles": {}}))
            proceed.set()
            t.join()

        self.assertEqual(state.load(max_age=60.0)["profiles"], {})

    @unittest.skipUnless(POSIX, "needs fcntl")
    def test_7_auth_locked_acquires_codex_auth_lock(self):
        home = self.box.home("work")
        write_auth(home, "acct-w", "w@example.com")
        with auth._locked(home):
            with open(home / "auth.lock", "a+") as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


# ====================================================================== Auto-resume, Redemption, Procs, Proxy (#4, #5, #6, #8, #9, #15, M2)
class RuntimeAndClassifierTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.close()

    def test_4_m2_15_classify_and_turn_span_attribution(self):
        th = {"id": "th1", "cwd": "/tmp", "updated_at": 1000}
        # Quota codes in 0.169.0
        for code_key, code_val in (
            ("codex_error_info", "usage_limit_exceeded"),
            ("codexErrorInfo", "usageLimitExceeded"),
            ("codexErrorInfo", "rateLimitExceeded"),
        ):
            ev = [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1",
                                                    "error": {"message": "limit", code_key: code_val}}}]
            self.assertEqual(auto_resume.classify(th, ev, []).classification, auto_resume.QUOTA_5H)

        # Dict CodexErrorInfo + healthy live turn wins over old rollout
        old_ev = [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old",
                                                    "error": {"codex_error_info": {"httpConnectionFailed": {}}, "message": "net"}}}]
        live_ok = {"status": {"type": "idle"}, "turns": [{"id": "new", "status": "completed", "error": None}]}
        self.assertFalse(auto_resume.classify(th, old_ev, [], live_thread=live_ok).halted)

        # Trailing turn_aborted -> interrupted OTHER
        aborted = [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1"}},
                   {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "t2", "reason": "interrupted"}}]
        d_abort = auto_resume.classify(th, aborted, [])
        self.assertEqual((d_abort.halted, d_abort.classification, d_abort.turn_id), (True, auto_resume.OTHER, "t2"))

        # Approval block after new task_started even when older turn completed
        appr = [
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-old"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-new"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c1", "input": "require_escalated"}},
        ]
        self.assertEqual(auto_resume.classify(th, appr, []).classification, auto_resume.APPROVAL_BLOCKED)

        # #15: old rollout does not bleed into newer live modelNotFound; subsecond ts_nanos respected
        live_mnf = {"id": "th1", "status": {"type": "idle"},
                    "turns": [{"id": "t-new", "status": "failed", "error": {"codexErrorInfo": "modelNotFound", "message": "nf"}}]}
        self.assertNotEqual(auto_resume.classify(th, old_ev, [], live_thread=live_mnf).classification, auto_resume.QUOTA_5H)

        events_sub = [{"timestamp": "2026-11-26T10:00:00.900Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2"}}]
        sec = int(auto_resume._event_ts(events_sub[0]))
        self.assertFalse(auto_resume._has_429_evidence(events_sub, [{"id": 1, "ts": sec, "ts_nanos": 100_000_000, "body": "status=429"}], turn_id="t2"))
        self.assertTrue(auto_resume._has_429_evidence(events_sub, [{"id": 2, "ts": sec, "ts_nanos": 950_000_000, "body": "status=429"}], turn_id="t2"))

    def test_5_execute_due_atomic_claim_and_retry_budget(self):
        cfg = config._merge(config.DEFAULTS, {"auto_resume": {"enabled": True, "max_retries_per_turn": 2, "backoff_seconds": 10}})
        diags = [auto_resume.Diagnosis("th1", "t1", True, auto_resume.QUOTA_5H, "5h limit"),
                 auto_resume.Diagnosis("th2", "t2", True, auto_resume.QUOTA_5H, "5h limit")]
        res = auto_resume.WindowResolution(True, 100, "acct", False)
        with mock.patch.object(auto_resume, "diagnose", return_value=diags[:1]),              mock.patch.object(auto_resume, "resolve_window", return_value=res),              mock.patch.object(auto_resume, "remote_cmd") as rc:
            # Connection failure is not the turn's fault: it never spends the retry budget.
            rc.server_socket.side_effect = SystemExit("no sock")
            for now in (100, 110, 120):
                out = auto_resume.execute_due(cfg, now=now)
                self.assertEqual([o["action"] for o in out], ["error"])
            entry = state.auto_resume_entries()["th1"]
            self.assertEqual((entry["status"], entry["attempts"]), ("pending", 0))

        # A per-thread failure spends one attempt; after the budget the entry becomes failed.
        failing = [{"id": "th1", "turns": [{"id": "t1", "status": "failed"}], "status": {"type": "idle"}}]
        with mock.patch.object(auto_resume, "diagnose", return_value=diags[:1]),              mock.patch.object(auto_resume, "resolve_window", return_value=res),              mock.patch.object(config, "load", return_value=cfg),              mock.patch.object(auto_resume.appserver, "AppServer"),              mock.patch.object(auto_resume.appserver, "read_thread_with_turns",
                               side_effect=appserver.AppServerError("boom")),              mock.patch.object(auto_resume, "remote_cmd") as rc:
            rc.server_socket.return_value = ("sock", None)
            self.assertEqual(auto_resume.execute_due(cfg, now=200)[0]["action"], "error")
            self.assertEqual(auto_resume.execute_due(cfg, now=210)[0]["action"], "error")
            self.assertEqual(state.auto_resume_entries()["th1"]["attempts"], 2)
            self.assertEqual(auto_resume.execute_due(cfg, now=220)[0]["action"], "failed")
            self.assertEqual(state.auto_resume_entries()["th1"]["status"], "failed")

    def test_5_execute_due_reports_each_thread_once(self):
        cfg = config._merge(config.DEFAULTS, {"auto_resume": {"enabled": True, "max_retries_per_turn": 3}})
        diags = [auto_resume.Diagnosis("th1", "t1", True, auto_resume.QUOTA_5H, "5h limit"),
                 auto_resume.Diagnosis("th2", "t2", True, auto_resume.QUOTA_5H, "5h limit")]
        res = auto_resume.WindowResolution(True, 100, "acct", False)
        ok = {"id": "th1", "turns": [{"id": "t1", "status": "failed"}], "status": {"type": "idle"}}
        with mock.patch.object(auto_resume, "diagnose", return_value=diags),              mock.patch.object(auto_resume, "resolve_window", return_value=res),              mock.patch.object(config, "load", return_value=cfg),              mock.patch.object(auto_resume.appserver, "AppServer"),              mock.patch.object(auto_resume.appserver, "read_thread_with_turns",
                               side_effect=[ok, appserver.AppServerClosedError("control socket closed")]),              mock.patch.object(auto_resume, "remote_cmd") as rc:
            rc.server_socket.return_value = ("sock", None)
            rc.dispatch_turn.return_value = "new-turn"
            out = auto_resume.execute_due(cfg, now=100)
        self.assertEqual([(o["thread_id"], o["action"]) for o in out], [("th1", "resumed"), ("th2", "error")])

    def test_diagnose_window_wraps_and_light_threads_skip_first_message(self):
        rows = [{"id": f"t{i}", "updated_at": 10 - i, "archived": 0, "rollout_path": None, "source": "cli"}
                for i in range(5)]
        seen = []
        with mock.patch.object(codex, "threads", side_effect=lambda **kw: (seen.append(kw), rows)[1]),              mock.patch.object(codex, "recent_thread_logs", return_value=[]),              mock.patch.object(auto_resume, "_capable_pool_all_limited", return_value=False),              mock.patch.object(auto_resume, "classify") as cls:
            cls.side_effect = lambda row, *a, **k: auto_resume.Diagnosis(row["id"], None, True, auto_resume.OTHER, "x")
            out = auto_resume.diagnose({}, all_threads=False, limit=3, offset=0, thread_ids=[r["id"] for r in rows],
                                       live_threads={})
            self.assertEqual([d.thread_id for d in out], ["t0", "t1", "t2"])
            out = auto_resume.diagnose({}, limit=3, offset=4, thread_ids=[r["id"] for r in rows], live_threads={})
            self.assertEqual([d.thread_id for d in out], ["t4", "t0", "t1"])
        self.assertTrue(all(kw.get("light") for kw in seen))
        with sqlite3.connect(self.box.shared / "state_5.sqlite") as conn:
            conn.execute("create table threads (id text primary key, updated_at integer, first_user_message text)")
            conn.execute("insert into threads values ('a', 1, 'hello')")
        self.assertNotIn("first_user_message", codex.threads(light=True)[0])
        self.assertIn("first_user_message", codex.threads()[0])

    def test_6_redemption_clears_key_before_snapshot_error_and_preserves_pause(self):
        prof = profiles.Profile("p1", self.box.home("p1"))
        write_auth(prof.home, "acct-1", "a@example.com")
        state.block("p1", time.time() + 3600, "quota: limited")
        state.reset_attempt("p1")

        mock_srv = mock.MagicMock()
        mock_srv.__enter__ = mock.MagicMock(return_value=mock_srv)
        mock_srv.__exit__ = mock.MagicMock(return_value=False)
        mock_srv.result.side_effect = lambda m, p=None: (
            {"outcome": "reset"} if m == "account/rateLimitResetCredit/consume"
            else (_ for _ in ()).throw(appserver.AppServerError("read failed")) if m == "account/rateLimits/read"
            else {}
        )
        with mock.patch("mycodex.redemption.AccountServer", return_value=mock_srv), \
             mock.patch("mycodex.redemption._lock", return_value=contextlib.nullcontext()):
            with self.assertRaises(appserver.AppServerError):
                redemption.consume(prof)
        self.assertIsNone(state.load()["profiles"]["p1"].get("reset_attempt"))
        self.assertIn("p1", state.blocks())

    def test_8_procs_classify_excludes_helper_binaries(self):
        mk = lambda argv: procs.Proc(pid=1, ppid=1, argv=argv, env={}, started=time.time(), tty=False)
        self.assertEqual(procs.classify(mk(["/opt/bin/codex-code-mode-host", "--listen", "grpc+stdio://"])), "")
        self.assertEqual(procs.classify(mk(["/opt/bin/codex-exec-server"])), "")
        self.assertEqual(procs.classify(mk(["/usr/local/bin/codex"])), "codex tui")

    def test_9_proxy_classify_curly_apostrophe(self):
        body = json.dumps({"detail": "You\u2019ve hit your usage limit for GPT-4o"}).encode()
        self.assertEqual(proxy.classify(403, body).kind, "quota")


# ====================================================================== Protocol, Launch, Proxy, Maintenance, Codex (#10-#14, #16-#17, #19-#20, M4-M5)
class ProtocolAndMaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.close()

    def test_10_paginated_hydration_and_cursor_validation(self):
        srv = mock.MagicMock()
        srv.result.side_effect = [
            {"thread": {"id": "t1", "turns": []}},
            {"data": [], "nextCursor": "c1"},
            {"data": [], "nextCursor": "c1"},
        ]
        with self.assertRaises(appserver.AppServerError):
            appserver.read_thread_with_turns(srv, "t1", limit=2)

        srv2 = mock.MagicMock()
        srv2.result.side_effect = [{"thread": {"id": "t1", "turns": []}}, {}]
        with self.assertRaises(appserver.AppServerError):
            appserver.read_thread_with_turns(srv2, "t1", limit=2)

    def test_11_wants_proxy_flags_and_subcommands(self):
        for off in (["archive"], ["exec-server"], ["-C", "/w", "login"], ["exec", "--help"],
                    ["-i", "a.png", "b.png", "--help"], ["app-server", "--print-startup-config"]):
            self.assertFalse(launch.wants_proxy(off), off)
        for on in (["agents"], ["-C", "/w", "agents"], ["--", "archive"],
                   ["app-server", "--startup-client-info", '{"name":"local"}'], ["--print-startup-config"]):
            self.assertTrue(launch.wants_proxy(on), on)

    def test_12_websocket_pong_and_disconnect_handling(self):
        server = appserver.AppServer("/unused")
        sent = []
        server._write = sent.append
        ping = bytes([0x89, 2]) + b"hi"
        msg = json.dumps({"id": 1, "result": {}}).encode()
        server._buf = ping + bytes([0x81, len(msg)]) + msg
        self.assertEqual(server._receive(time.time() + 5), {"id": 1, "result": {}})
        self.assertEqual(sent[0][0], 0x8A)

        server._buf = bytes([0x88, 0])
        with self.assertRaises(appserver.AppServerClosedError):
            server._receive(time.time() + 5)
        server._receive = mock.Mock(side_effect=appserver.AppServerClosedError("control socket closed"))
        with self.assertRaises(appserver.AppServerError):
            server.wait_for("turn/completed", timeout=5)

    def test_13_proxy_models_excludes_auth_invalid_and_locks_pool(self):
        write_auth(self.box.home("bad"), "acct-b", "b@example.com")
        write_auth(self.box.home("good"), "acct-g", "g@example.com")
        state.save_quota("bad", {"profile": "bad", "fetched_at": int(time.time()), "ok": False, "status": "auth invalid", "windows": []})
        state.save_quota("good", {"profile": "good", "fetched_at": int(time.time()), "ok": True, "status": "ready", "windows": []})
        rp = proxy.RotationProxy("bad", role="session")
        try:
            tried = []
            handler = proxy.Handler.__new__(proxy.Handler)
            handler.server = mock.Mock(proxy=rp)
            handler.command = "GET"
            handler.path = "/backend-api/codex/models"
            handler.headers = {}
            handler._attempt = lambda prof, *a: (tried.append(prof.name), None)[1]
            handler._proxy(None)
            self.assertEqual(tried, ["good"])
        finally:
            rp.server.server_close()

    def test_14_maintenance_literal_profiles_root(self):
        custom_shared = self.box.shared.parent / "custom_shared"
        custom_profiles = custom_shared / "profiles"
        other_profiles = self.box.shared.parent / "customXshared" / "profiles"
        tid = "01a20366-d430-7630-a508-451b45bcd38d"
        (custom_shared / "sessions").mkdir(parents=True)
        real = custom_shared / "sessions" / f"rollout-{tid}.jsonl"
        real.write_text(json.dumps({"type": "session_meta", "payload": {"id": tid}}) + "\n")
        with sqlite3.connect(custom_shared / "state_5.sqlite") as conn:
            conn.execute("create table threads (id text primary key, rollout_path text)")
            conn.execute("insert into threads values (?, ?)", (tid, str(custom_profiles / "gone" / "sessions" / real.name)))
            conn.execute("insert into threads values (?, ?)", ("01a20366-d430-7630-a508-451b45bcd38e", str(other_profiles / "gone" / "sessions" / real.name)))
        with mock.patch.object(paths, "SHARED_CODEX_HOME", custom_shared), \
             mock.patch.object(paths, "PROFILES_ROOT", custom_profiles):
            res = maintenance.audit(fix=True)
        self.assertEqual((res[0]["legacy"], res[0]["repaired"]), (1, 1))

    def test_16_17_codex_version_fallback_and_sqlite_warnings(self):
        (self.box.shared / "version.json").write_text("{corrupt")
        bad = self.box.home("bad")
        bad.mkdir(parents=True)
        (bad / "version.json").write_bytes(b"\xff\xfe")
        good = self.box.home("good")
        good.mkdir(parents=True)
        (good / "version.json").write_text(json.dumps({"latest_version": "0.169.0"}))
        with mock.patch.object(codex, "resolved_binary", return_value=None):
            self.assertEqual(codex.package_info().get("update_check"), {"latest_version": "0.169.0"})

        (self.box.shared / "state_5.sqlite").write_text("corrupt")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(codex.threads(), [])
        self.assertIn("sqlite", err.getvalue().lower())

    def test_19_m4_profile_remove_and_stop_failure(self):
        write_auth(self.box.home("relay"), "acct-r", "r@example.com")
        config.save(config._merge(config.DEFAULTS, {"remote": {"profile": "relay"}}), paths.CONFIG_FILE)
        with mock.patch.object(remote, "service_info", return_value={"ActiveState": "reloading"}), \
             mock.patch.object(accounts, "remote_stop") as stop_mock, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(accounts.profile_remove("relay", False, True, False), 1)
        stop_mock.assert_not_called()

        paths.REMOTE_UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.REMOTE_UNIT_FILE.write_text("unit")
        fail = subprocess.CompletedProcess(["systemctl"], 1, "", "failed")
        with mock.patch.object(remote, "_systemctl", return_value=fail):
            self.assertEqual(remote_cmd.stop(quiet=True), 1)

    def test_20_m5_literal_find_threads_limit_zero_and_body_bounds(self):
        with sqlite3.connect(self.box.shared / "state_5.sqlite") as conn:
            conn.execute("create table threads (id text primary key, updated_at integer, archived integer)")
            conn.execute("insert into threads values ('abcd-1', 100, 0)")
        self.assertEqual(codex.find_threads("abc_"), [])
        self.assertEqual(codex.find_threads("ABC"), [])
        self.assertEqual(len(codex.find_threads("abcd")), 1)
        self.assertEqual(auto_resume.diagnose(config._merge(config.DEFAULTS, {}), all_threads=True, limit=0, live_threads={}), [])

        h = proxy.Handler.__new__(proxy.Handler)
        h.command = "POST"
        h.rfile = io.BytesIO(b"")
        for bad_hdrs in (
            {"Content-Length": "-1"},
            {"Content-Length": str(proxy.MAX_REQUEST_BODY + 1)},
            {"Transfer-Encoding": "chunked"},
        ):
            h.headers = bad_hdrs
            if bad_hdrs.get("Transfer-Encoding"):
                h.rfile = io.BytesIO(f"{proxy.MAX_REQUEST_BODY + 1:x}\r\n".encode())
            with self.assertRaises(ValueError):
                h._read_body()


if __name__ == "__main__":
    unittest.main()
