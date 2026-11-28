"""Tests for v0.4.8 remote enhancements: auto-restart, remote doctor, sandbox/approval/trust flags."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from test_core import PLUS, Sandbox, write_auth
from test_improvements import RPC
from mycodex import cli, codex, config, paths, profiles, projects, quota, remote, remote_cmd


class SandboxTrustAndLogTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.close()

    def test_config_validates_sandbox_and_approval(self):
        p = self.box.shared / "settings.json"
        p.write_text(json.dumps({"remote": {"sandbox": "danger-full-access", "approval": "never"}}))
        cfg = config.load(p)
        self.assertEqual(cfg["remote"]["sandbox"], "danger-full-access")
        self.assertEqual(cfg["remote"]["approval"], "never")
        for bad in ({"remote": {"sandbox": "bogus"}}, {"remote": {"approval": "bogus"}}):
            p.write_text(json.dumps(bad))
            with self.assertRaises(SystemExit):
                config.load(p)

    def test_server_args_includes_sandbox_and_approval(self):
        args = remote.server_args("rotating", "http://127.0.0.1:9/backend-api/codex",
                                  sandbox="danger-full-access", approval="never")
        self.assertEqual(args, [
            "-c", 'openai_base_url="http://127.0.0.1:9/backend-api/codex"',
            "-c", 'sandbox_mode="danger-full-access"',
            "-c", 'approval_policy="never"',
            "remote-control",
        ])
        self.assertEqual(remote.server_args("pinned", None, sandbox="workspace-write"),
                         ["-c", 'sandbox_mode="workspace-write"', "remote-control"])

    def test_trust_project_is_idempotent_and_atomic(self):
        cfg_toml = self.box.shared / "config.toml"
        cfg_toml.write_text('model = "gpt-6.2-sol"\n\n[projects."/other"]\ntrust_level = "untrusted"\n')
        target = self.box.shared / "proj"
        target.mkdir()
        self.assertTrue(codex.trust_project(target, cfg_toml))
        self.assertFalse(codex.trust_project(target, cfg_toml))
        trusted = codex.trusted_projects(cfg_toml)
        self.assertEqual(trusted[str(target.resolve())], "trusted")
        self.assertEqual(trusted["/other"], "untrusted")
        # Updating an existing table updates rather than duplicating the header
        self.assertTrue(codex.trust_project(Path("/other"), cfg_toml))
        self.assertEqual(cfg_toml.read_text().count('[projects."/other"]'), 1)
        self.assertEqual(codex.trusted_projects(cfg_toml)["/other"], "trusted")
        # Single-quoted, whitespace/comment headers and symlinked paths never produce duplicates
        alt = self.box.shared / "alt"
        alt.mkdir()
        link = self.box.shared / "alt-link"
        link.symlink_to(alt)
        cfg_toml.write_text(f"[ projects.'{alt.resolve()}' ] # keep\ntrust_level = \"untrusted\"\n")
        self.assertTrue(codex.trust_project(link, cfg_toml))
        self.assertEqual(cfg_toml.read_text().count("trust_level"), 1)
        self.assertFalse(codex.trust_project(alt, cfg_toml))
        # Inline/dotted [projects] definition refuses duplicate insertion cleanly
        inline = self.box.shared / "inline"
        inline.mkdir()
        cfg_toml.write_text(f'[projects]\n"{inline.resolve()}" = {{ trust_level = "untrusted" }}\n')
        with self.assertRaises(OSError):
            codex.trust_project(inline, cfg_toml)

    def test_recent_proxy_errors_handles_missing_empty_and_errors(self):
        import datetime
        log = self.box.shared / "proxy.log"
        self.assertEqual(codex.recent_proxy_errors(path=log), [])
        log.write_text("")
        self.assertEqual(codex.recent_proxy_errors(path=log), [])
        now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_line = f"{now_ts} status=502 error upstream"
        log.write_text(f"2020-01-01T00:00:00Z status=502 error stale\n{now_ts} ok path=/models\n{recent_line}\n")
        self.assertEqual(codex.recent_proxy_errors(path=log),
                         ["2020-01-01T00:00:00Z status=502 error stale", recent_line])
        self.assertEqual(codex.recent_proxy_errors(path=log, since_seconds=3600), [recent_line])

    def test_sandbox_status_detects_missing_bwrap(self):
        with mock.patch("mycodex.paths.which", return_value=None), \
             mock.patch("mycodex.codex.recent_log_errors", return_value=[]):
            sb = codex.sandbox_status()
        self.assertFalse(sb["ready"])
        self.assertIn("bwrap is not on PATH", sb["summary"])


class RemoteSeedAutoRestartAndDoctorTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        home = self.box.home("work")
        write_auth(home, "work-tok", "work@example.com")
        config.save(config._merge(config.DEFAULTS, {"active": "work", "remote": {"profile": "work"}}))

    def tearDown(self):
        self.box.close()

    def test_foreground_rejects_sandbox_and_approval(self):
        for flag in ("--sandbox", "--approval"):
            with mock.patch("mycodex.remote_cmd.foreground") as fg, \
                 mock.patch("mycodex.paths.migrate_legacy", return_value=[]), \
                 contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as ctx:
                cli.main(["remote-control", "--foreground", flag, "never"])
            self.assertEqual(ctx.exception.code, 2)
            fg.assert_not_called()

    def test_remote_start_persists_sandbox_approval_and_warns_when_unready(self):
        ready = quota.parse("work", PLUS)
        fake_res = subprocess.CompletedProcess(["systemctl"], 0, stdout="", stderr="")
        with mock.patch("mycodex.quota.fetch", return_value=ready), \
             mock.patch("mycodex.procs.scan", return_value=[]), \
             mock.patch("mycodex.remote.install_unit", return_value=False), \
             mock.patch("mycodex.remote._systemctl", return_value=fake_res), \
             mock.patch("mycodex.remote.service_info", return_value={"LoadState": "loaded", "ActiveState": "inactive"}), \
             mock.patch("mycodex.codex.sandbox_status", return_value={"ready": False, "summary": "bwrap missing"}), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            rc = remote_cmd.start(wait=False, sandbox="workspace-write", approval="never")
        self.assertEqual(rc, 0)
        self.assertIn("sandbox readiness", err.getvalue())
        loaded = config.load()
        self.assertEqual(loaded["remote"]["sandbox"], "workspace-write")
        self.assertEqual(loaded["remote"]["approval"], "never")

    def test_projects_add_auto_restarts_only_when_created_and_enabled(self):
        proj_dir = self.box.shared / "myproj"
        proj_dir.mkdir()
        unit = self.box.shared / "mycodex-remote.service"
        unit.write_text("# Managed by mycodex\n")
        fake_ok = subprocess.CompletedProcess(["systemctl"], 0, stdout="", stderr="")

        def _run_add(existing: bool, restart: bool = True, trust: bool = False):
            responses = {"project/list": {"data": [{"id": "p1", "name": "myproj", "roots": [{"path": str(proj_dir.resolve())}]}]}} if existing \
                else {"project/list": {"data": []}, "project/create": {"project": {"id": "p1", "name": "myproj", "roots": [{"path": str(proj_dir.resolve())}]}}}
            server = RPC(responses)
            with mock.patch.object(paths, "REMOTE_UNIT_FILE", unit), \
                 mock.patch.object(projects, "connection", return_value=server), \
                 mock.patch("mycodex.remote.service_info", return_value={"LoadState": "loaded", "ActiveState": "active"}), \
                 mock.patch("mycodex.remote._systemctl", return_value=fake_ok) as sysctl, \
                 mock.patch("mycodex.remote.wait_until_connected", return_value={"status": {"status": "connected"}}), \
                 contextlib.redirect_stdout(io.StringIO()) as out, \
                 contextlib.redirect_stderr(io.StringIO()):
                rc = projects.add(str(proj_dir), as_json=True, restart=restart, trust=trust)
            return rc, json.loads(out.getvalue()), sysctl

        rc, payload, sysctl = _run_add(existing=False, restart=True, trust=True)
        self.assertEqual(rc, 0)
        self.assertTrue(payload["created"])
        self.assertTrue(payload["trusted"])
        self.assertTrue(payload["restarted"])
        sysctl.assert_called_once_with("restart", paths.REMOTE_UNIT)

        rc, payload, sysctl = _run_add(existing=True, restart=True)
        self.assertEqual(rc, 0)
        self.assertFalse(payload["created"])
        self.assertFalse(payload["restarted"])
        sysctl.assert_not_called()

        rc, payload, sysctl = _run_add(existing=False, restart=False)
        self.assertEqual(rc, 0)
        self.assertFalse(payload["restarted"])
        sysctl.assert_not_called()

    def test_seed_passes_sandbox_approval_trust_and_auto_restarts(self):
        proj_dir = self.box.shared / "seedproj"
        proj_dir.mkdir()
        unit = self.box.shared / "mycodex-remote.service"
        unit.write_text("# Managed by mycodex\n")
        fake_ok = subprocess.CompletedProcess(["systemctl"], 0, stdout="", stderr="")
        calls = []

        class FakeServer:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def result(self, method, params):
                calls.append((method, params))
                if method == "project/list":
                    return {"data": []}
                if method == "project/create":
                    return {"project": {"id": "p-seed", "name": "seedproj", "roots": [{"path": str(proj_dir.resolve())}]}}
                if method == "thread/start":
                    return {"thread": {"id": "t-seed", "modelProvider": "openai"}}
                if method == "turn/start":
                    return {"turn": {"id": "turn-seed"}}
                return {}
            def wait_for(self, *a, **kw):
                return {"params": {"threadId": "t-seed", "turn": {"id": "turn-seed", "status": "completed"}}}

        with mock.patch.object(paths, "REMOTE_UNIT_FILE", unit), \
             mock.patch("mycodex.remote_cmd.server_socket", return_value=("/sock", "work")), \
             mock.patch("mycodex.appserver.AppServer", return_value=FakeServer()), \
             mock.patch("mycodex.remote.service_info", return_value={"LoadState": "loaded", "ActiveState": "active"}), \
             mock.patch("mycodex.remote._systemctl", return_value=fake_ok) as sysctl, \
             mock.patch("mycodex.remote.wait_until_connected", return_value={"status": {"status": "connected"}}), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["remote", "seed", str(proj_dir), "--sandbox", "danger-full-access",
                           "--approval", "never", "--trust"])
        self.assertEqual(rc, 0)
        start_params = [p for m, p in calls if m == "thread/start"][0]
        self.assertEqual(start_params["sandbox"], "danger-full-access")
        self.assertEqual(start_params["approvalPolicy"], "never")
        self.assertEqual(codex.trusted_projects().get(str(proj_dir.resolve())), "trusted")
        sysctl.assert_called_once_with("restart", paths.REMOTE_UNIT)
        self.assertIn("restarted (connected)", out.getvalue())

        # --no-wait skips restart so the in-flight first turn is not interrupted
        with mock.patch.object(paths, "REMOTE_UNIT_FILE", unit), \
             mock.patch("mycodex.remote_cmd.server_socket", return_value=("/sock", "work")), \
             mock.patch("mycodex.appserver.AppServer", return_value=FakeServer()), \
             mock.patch("mycodex.remote.service_info", return_value={"LoadState": "loaded", "ActiveState": "active"}), \
             mock.patch("mycodex.remote._systemctl", return_value=fake_ok) as sysctl, \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            rc = remote_cmd.seed(str(proj_dir), None, None, wait=False)
        self.assertEqual(rc, 0)
        sysctl.assert_not_called()
        self.assertIn("skipped while first turn is running", out.getvalue())

        # wait=True timeout/Ctrl-C ("running on the server") also skips restart so turn survives
        with mock.patch.object(paths, "REMOTE_UNIT_FILE", unit), \
             mock.patch("mycodex.remote_cmd.server_socket", return_value=("/sock", "work")), \
             mock.patch("mycodex.appserver.AppServer", return_value=FakeServer()), \
             mock.patch("mycodex.remote_cmd._wait_for_turn", return_value="running on the server"), \
             mock.patch("mycodex.remote.service_info", return_value={"LoadState": "loaded", "ActiveState": "active"}), \
             mock.patch("mycodex.remote._systemctl", return_value=fake_ok) as sysctl, \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()):
            rc = remote_cmd.seed(str(proj_dir), None, None, wait=True)
        self.assertEqual(rc, 0)
        sysctl.assert_not_called()

    def test_feed_or_seed_rejects_seed_only_flags_on_thread_id(self):
        tid = "01a22083-d628-78d3-a89f-505e970dee5f"
        with mock.patch("mycodex.remote_cmd.feed_thread") as feed, \
             contextlib.redirect_stderr(io.StringIO()) as err:
            rc = remote_cmd.feed_or_seed(tid, None, "hi", sandbox="danger-full-access", trust=True)
        self.assertEqual(rc, 2)
        feed.assert_not_called()
        self.assertIn("--sandbox", err.getvalue())
        self.assertIn("--trust", err.getvalue())

    def test_remote_doctor_json_reports_checks(self):
        proj_dir = self.box.shared / "docproj"
        proj_dir.mkdir()
        collect_data = {
            "config": {"mode": "rotating", "profile": "work", "cwd": str(self.box.shared),
                       "sandbox": None, "approval": None},
            "service": {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "UnitFileState": "enabled"},
            "servers": [{"kind": "service", "pid": 123, "profile": "work", "socket": "/tmp/sock", "uptime": "1m",
                         "status": {"status": "connected"}, "clients": [{"displayName": "Android 16", "appVersion": "1.2026.307", "lastSeenAt": 1}]}],
            "serve_state": {"proxy_port": 41000},
        }
        rpc = RPC({
            "project/list": {"data": [{"id": "p1", "name": "docproj", "roots": [{"path": str(proj_dir.resolve())}]}]},
            "thread/list": {"data": [{"id": "t1234567890", "cwd": str(proj_dir.resolve()), "projectId": "p1"}]},
            "thread/loaded/list": {"data": []},
        })
        with mock.patch("mycodex.remote_cmd.collect", return_value=collect_data), \
             mock.patch("mycodex.quota.fetch", return_value=quota.parse("work", PLUS)), \
             mock.patch("mycodex.proxy.health", return_value={"current": "work", "requests": 2}), \
             mock.patch("mycodex.appserver.AppServer", return_value=rpc), \
             mock.patch("mycodex.codex.sandbox_status", return_value={"ready": False, "summary": "bwrap not on PATH"}), \
             mock.patch("mycodex.codex.recent_log_errors", return_value=[]), \
             mock.patch("mycodex.codex.recent_proxy_errors", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(["remote", "doctor", "--json"])
        self.assertEqual(rc, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["result"], "warn")
        checks = {c["check"]: c for c in report["checks"]}
        self.assertEqual(checks["remote link"]["result"], "ok")
        self.assertEqual(checks["paired clients"]["result"], "ok")
        self.assertEqual(checks["sandbox / bwrap"]["result"], "warn")
        self.assertIn("logs / errors", checks)
