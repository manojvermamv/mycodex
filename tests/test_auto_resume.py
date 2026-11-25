"""Isolated tests for opt-in auto-resume diagnosis, scheduling, and thread feeding."""

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from test_core import PLUS, Sandbox, jwt, write_auth
from mycodex import appserver, auto_resume, cli, config, profiles, quota, remote_cmd, state


TID1 = "01a20366-d430-7630-a508-451b45bcd38d"
TID2 = "01a20366-d430-7630-a508-451b45bcd38e"


def _limited_quota(name: str, reset_at: int, plan: str = "plus") -> quota.Quota:
    return quota.Quota(
        profile=name, ok=True, status="limited", plan=plan,
        windows=[quota.Window("5h", 18000, 100.0, reset_at),
                 quota.Window("weekly", 604800, 20.0, reset_at + 100000)],
        usage_allowed=False,
    )


def _ready_quota(name: str, remaining_pct: float = 100.0, plan: str = "plus") -> quota.Quota:
    return quota.Quota(
        profile=name, ok=True, status="ready", plan=plan,
        windows=[quota.Window("5h", 18000, 100.0 - remaining_pct, 9999999999),
                 quota.Window("weekly", 604800, 10.0, 9999999999)],
        usage_allowed=True,
    )


class _MultiThreadRPC:
    def __init__(self, thread_map=None, new_turn_id="turn-new-1"):
        self.thread_map = thread_map or {}
        self.new_turn_id = new_turn_id
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def result(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if method == "thread/read":
            tid = params["threadId"]
            return {"thread": self.thread_map[tid]}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "status": {"type": "idle"}}}
        if method == "turn/start":
            return {"turn": {"id": self.new_turn_id}}
        return {}


class AutoResumeConfigAndFeedTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.close()

    def test_auto_resume_defaults_to_disabled(self):
        cfg = config.load(self.box.shared / "missing-config.json")
        self.assertFalse(cfg["auto_resume"]["enabled"])
        self.assertEqual(cfg["auto_resume"]["threads"], [])
        self.assertFalse(auto_resume.is_enabled(cfg, TID1))

    def test_auto_resume_config_validation_rejects_bad_values(self):
        path = self.box.shared / "cfg.json"
        for bad in (
            {"auto_resume": "yes"},
            {"auto_resume": {"enabled": "true"}},
            {"auto_resume": {"threads": "id1"}},
            {"auto_resume": {"buffer_seconds": -1}},
            {"auto_resume": {"backoff_seconds": 0}},
            {"auto_resume": {"message": "   "}},
        ):
            path.write_text(json.dumps(bad))
            with self.assertRaises(SystemExit):
                config.load(path)

    def test_feed_command_targets_existing_thread_id_without_seeding(self):
        server = _MultiThreadRPC()
        row = {"id": TID1, "model_provider": "openai", "cwd": "/work/Salience", "archived": 0}
        with mock.patch("mycodex.threads._find", return_value=row), \
             mock.patch("mycodex.remote_cmd.server_socket", return_value=("/sock", "relay")), \
             mock.patch("mycodex.appserver.AppServer", return_value=server), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["feed", TID1, "--no-wait", "--message", "Try again"])
        self.assertEqual(rc, 0)
        methods = [m for m, _ in server.calls]
        self.assertEqual(methods, ["thread/resume", "turn/start"])
        self.assertNotIn("thread/start", methods)
        self.assertEqual(server.calls[1][1]["input"], [{"type": "text", "text": "Try again"}])

    def test_feed_command_accepts_positional_message(self):
        server = _MultiThreadRPC()
        row = {"id": TID1, "model_provider": "openai", "cwd": "/work/Salience", "archived": 0}
        with mock.patch("mycodex.threads._find", return_value=row), \
             mock.patch("mycodex.remote_cmd.server_socket", return_value=("/sock", "relay")), \
             mock.patch("mycodex.appserver.AppServer", return_value=server), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["feed", TID1, "Try again", "--no-wait"])
        self.assertEqual(rc, 0)
        self.assertEqual(server.calls[1][1]["input"], [{"type": "text", "text": "Try again"}])


class AutoResumeClassifierAndSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        write_auth(self.box.home("plus1"), "acct-1", "plus1@example.com")
        write_auth(self.box.home("plus2"), "acct-2", "plus2@example.com")
        write_auth(self.box.home("free1"), "acct-f", "free1@example.com")
        free_auth = json.loads((self.box.home("free1") / "auth.json").read_text())
        free_auth["tokens"]["id_token"] = jwt(
            email="free1@example.com",
            **{"https://api.openai.com/auth": {"chatgpt_plan_type": "free", "chatgpt_account_id": "acct-f"}},
        )
        (self.box.home("free1") / "auth.json").write_text(json.dumps(free_auth))
        self.cfg = config._merge(config.DEFAULTS, {
            "active": "plus1",
            "rotation": {"enabled": True, "order": ["plus1", "plus2", "free1"], "disabled": []},
        })

    def tearDown(self):
        self.box.close()

    def test_classifier_identifies_direct_429_approval_and_other(self):
        row = {"id": TID1, "cwd": "/work/Salience", "updated_at": 1000}

        # (a) Direct 429 usage limit error
        ev_429 = [{"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "t-429",
            "error": {"message": "You've hit your usage limit. try again at 8:57 PM.", "codex_error_info": "usage_limit_reached"},
        }}]
        d_a = auto_resume.classify(row, ev_429, [])
        self.assertTrue(d_a.halted)
        self.assertEqual(d_a.classification, auto_resume.QUOTA_5H)

        # (a) Cascading model_not_found from Free fallback while Plus pool is 5h-limited
        ev_cascade = [{"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "t-casc",
            "error": {"message": "The 'gpt-6.2-sol' model is not supported when using Codex with a ChatGPT account.",
                      "codex_error_info": "model_not_found"},
        }}]
        d_casc = auto_resume.classify(row, ev_cascade, [], pool_all_limited=True)
        self.assertTrue(d_casc.halted)
        self.assertEqual(d_casc.classification, auto_resume.QUOTA_5H)

        # (b) waitingOnApproval live status
        live_app = {"id": TID1, "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
                    "turns": [{"id": "t-app", "status": "inProgress"}]}
        d_b = auto_resume.classify(row, [], [], live_thread=live_app)
        self.assertTrue(d_b.halted)
        self.assertEqual(d_b.classification, auto_resume.APPROVAL_BLOCKED)

        # (c) Other non-quota failure when pool is not limited
        d_c = auto_resume.classify(row, ev_cascade, [], pool_all_limited=False)
        self.assertTrue(d_c.halted)
        self.assertEqual(d_c.classification, auto_resume.OTHER)

    def test_disabled_never_schedules_or_feeds_even_when_quota_open(self):
        rollout = self.box.shared / "r1.jsonl"
        rollout.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "t1",
            "error": {"message": "You've hit your usage limit.", "codex_error_info": "usage_limit_reached"},
        }}) + "\n")
        row = {"id": TID1, "cwd": "/work", "updated_at": 1000, "archived": 0, "rollout_path": str(rollout)}
        quotas = {"plus1": _ready_quota("plus1"), "plus2": _ready_quota("plus2"), "free1": _ready_quota("free1", plan="free")}
        with mock.patch("mycodex.codex.threads", return_value=[row]), \
             mock.patch("mycodex.codex.recent_thread_logs", return_value=[]), \
             mock.patch("mycodex.remote_cmd.server_socket") as sock_mock:
            outcomes = auto_resume.execute_due(self.cfg, now=2000, quotas=quotas, live_threads={})
        self.assertEqual(outcomes, [])
        self.assertEqual(state.auto_resume_entries(), {})
        sock_mock.assert_not_called()

    def test_approval_and_other_never_retry_even_when_enabled(self):
        cfg = config._merge(self.cfg, {"auto_resume": {"enabled": True}})
        rows = [
            {"id": TID1, "cwd": "/work", "updated_at": 2000, "archived": 0, "rollout_path": None},
            {"id": TID2, "cwd": "/work", "updated_at": 1900, "archived": 0, "rollout_path": None},
        ]
        live = {
            TID1: {"id": TID1, "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
                   "turns": [{"id": "t-app", "status": "inProgress"}]},
            TID2: {"id": TID2, "status": {"type": "idle"},
                   "turns": [{"id": "t-oth", "status": "failed", "error": {"message": "network reset"}}]},
        }
        quotas = {"plus1": _ready_quota("plus1"), "plus2": _ready_quota("plus2"), "free1": _ready_quota("free1", plan="free")}
        with mock.patch("mycodex.codex.threads", return_value=rows), \
             mock.patch("mycodex.codex.recent_thread_logs", return_value=[]), \
             mock.patch("mycodex.remote_cmd.server_socket") as sock_mock:
            outcomes = auto_resume.execute_due(cfg, now=3000, diagnose_all=True, quotas=quotas, live_threads=live)
        self.assertEqual(outcomes, [])
        self.assertEqual(state.auto_resume_entries(), {})
        sock_mock.assert_not_called()

    def test_schedules_after_reset_plus_buffer_backs_off_then_resumes_idempotently(self):
        cfg = config._merge(self.cfg, {"auto_resume": {"threads": [TID1], "buffer_seconds": 60, "backoff_seconds": 30}})
        rows = [{"id": TID1, "cwd": "/work", "updated_at": 1000, "archived": 0, "rollout_path": None}]
        live_failed = {
            TID1: {"id": TID1, "status": {"type": "idle"},
                   "turns": [{"id": "t-fail", "status": "failed", "error": {"message": "You've hit your usage limit."}}]},
        }
        reset_at = 5000
        limited = {"plus1": _limited_quota("plus1", reset_at), "plus2": _limited_quota("plus2", reset_at + 300),
                   "free1": _ready_quota("free1", plan="free")}
        server = _MultiThreadRPC(thread_map=live_failed, new_turn_id="t-resumed-99")

        with mock.patch("mycodex.codex.threads", return_value=rows), \
             mock.patch("mycodex.codex.recent_thread_logs", return_value=[]):
            # 1. Before reset_at: schedules at reset_at + 60 = 5060, does not feed
            out1 = auto_resume.execute_due(cfg, now=4000, quotas=limited, live_threads=live_failed)
            self.assertEqual(out1, [])
            job = state.auto_resume_entries()[TID1]
            self.assertEqual(job["status"], "pending")
            self.assertEqual(job["scheduled_for"], 5060)

            # 2. At 5060, quota still lagging limited -> backs off +30s to 5090
            with mock.patch("mycodex.remote_cmd.server_socket") as sock_mock:
                out2 = auto_resume.execute_due(cfg, now=5060, quotas=limited, live_threads=live_failed)
                sock_mock.assert_not_called()
            self.assertEqual(out2, [{"thread_id": TID1, "action": "backoff", "scheduled_for": 5090}])
            self.assertEqual(state.auto_resume_entries()[TID1]["scheduled_for"], 5090)

            # 3. At 5090, quota is open -> feeds "Try again" once and marks resumed
            ready = {"plus1": _ready_quota("plus1"), "plus2": _limited_quota("plus2", reset_at + 300),
                     "free1": _ready_quota("free1", plan="free")}
            with mock.patch("mycodex.config.load", return_value=cfg), \
                 mock.patch("mycodex.remote_cmd.server_socket", return_value=("/sock", "relay")), \
                 mock.patch("mycodex.appserver.AppServer", return_value=server):
                out3 = auto_resume.execute_due(cfg, now=5090, quotas=ready, live_threads=live_failed)
            self.assertEqual(out3, [{"thread_id": TID1, "action": "resumed", "turn_id": "t-resumed-99"}])
            self.assertEqual(state.auto_resume_entries()[TID1]["status"], "resumed")
            self.assertEqual(state.auto_resume_entries()[TID1]["resumed_turn_id"], "t-resumed-99")
            self.assertEqual(server.calls[2][1]["input"], [{"type": "text", "text": "Try again"}])

            # 4. Subsequent check with same failed turn state stays idempotent (never feeds a second time)
            out4 = auto_resume.execute_due(cfg, now=5200, quotas=ready, live_threads=live_failed)
            self.assertEqual(out4, [])

    def test_disable_cancels_pending_job(self):
        cfg_on = config._merge(self.cfg, {"auto_resume": {"threads": [TID1]}})
        rows = [{"id": TID1, "cwd": "/work", "updated_at": 1000, "archived": 0, "rollout_path": None}]
        live_failed = {
            TID1: {"id": TID1, "status": {"type": "idle"},
                   "turns": [{"id": "t-fail", "status": "failed", "error": {"message": "usage limit reached"}}]},
        }
        limited = {"plus1": _limited_quota("plus1", 5000), "plus2": _limited_quota("plus2", 5300),
                   "free1": _ready_quota("free1", plan="free")}
        with mock.patch("mycodex.codex.threads", return_value=rows), \
             mock.patch("mycodex.codex.recent_thread_logs", return_value=[]):
            auto_resume.execute_due(cfg_on, now=4000, quotas=limited, live_threads=live_failed)
            self.assertEqual(state.auto_resume_entries()[TID1]["status"], "pending")

        cfg_path = self.box.shared / "config.json"
        config.save(cfg_on, cfg_path)
        orig_load, orig_editing = config.load, config.editing
        with mock.patch("mycodex.config.load", side_effect=lambda p=cfg_path: orig_load(cfg_path)), \
             mock.patch("mycodex.config.editing", side_effect=lambda p=cfg_path: orig_editing(cfg_path)), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["auto-resume", "disable", TID1]), 0)
        self.assertEqual(orig_load(cfg_path)["auto_resume"]["threads"], [])
        self.assertEqual(state.auto_resume_entries()[TID1]["status"], "cancelled")
