import base64
import http.client
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mycodex import cli, config, launch, procs, profiles, proxy, quota, remote, resolve, ui  # noqa: E402

POSIX = os.name != "nt"
PLUS = {
    "account_id": "a", "email": "x@example.com", "plan_type": "plus",
    "rate_limit": {"allowed": True, "limit_reached": False,
                   "primary_window": {"limit_window_seconds": 18000, "used_percent": 1, "reset_at": 1},
                   "secondary_window": {"limit_window_seconds": 604800, "used_percent": 2, "reset_at": 2}},
    "rate_limit_reset_credits": {"available_count": 1},
}
FREE = {
    "plan_type": "free",
    "rate_limit": {"allowed": True, "limit_reached": False,
                   "primary_window": {"limit_window_seconds": 2592000, "used_percent": 0, "reset_at": 3},
                   "secondary_window": None},
}


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def jwt(**claims) -> str:
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


def write_auth(home: Path, account: str, email: str, expires_in: int = 10 * 86400, access: str | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    data = {"OPENAI_API_KEY": None, "auth_mode": "chatgpt", "last_refresh": "2026-01-01T00:00:00Z", "tokens": {
        "id_token": jwt(email=email, **{"https://api.openai.com/auth": {"chatgpt_plan_type": "plus",
                                                                       "chatgpt_account_id": account}}),
        "access_token": access or jwt(iat=now, exp=now + expires_in, sub=account),
        "refresh_token": f"refresh-{account}", "account_id": account}}
    (home / "auth.json").write_text(json.dumps(data))


class GlobalsTest(unittest.TestCase):
    def test_profile_then_codex_args(self):
        g = cli.parse_globals(["--profile", "work", "resume", "--last"])
        self.assertEqual(g.profile, "work")
        self.assertEqual(g.rest, ["resume", "--last"])

    def test_profile_equals_and_flags(self):
        g = cli.parse_globals(["--profile=dev", "--no-rotate", "--dry-run"])
        self.assertEqual((g.profile, g.rotate, g.dry_run, g.rest), ("dev", False, True, []))

    def test_double_dash_is_verbatim(self):
        g = cli.parse_globals(["--profile", "a", "--", "--help"])
        self.assertTrue(g.passthrough_only)
        self.assertEqual(g.rest, ["--help"])

    def test_codex_flags_stop_global_parsing(self):
        g = cli.parse_globals(["-m", "gpt", "--profile", "x"])
        self.assertIsNone(g.profile)
        self.assertEqual(g.rest, ["-m", "gpt", "--profile", "x"])

    def test_help_only_before_command(self):
        self.assertTrue(cli.parse_globals(["--help"]).help)
        g = cli.parse_globals(["exec", "--help"])
        self.assertFalse(g.help)
        self.assertEqual(g.rest, ["exec", "--help"])


class ResolveTest(unittest.TestCase):
    names = ["work_example.com", "personal_example.com"]

    def test_variants(self):
        cfg = {"aliases": {"phone": "work_example.com"}}
        self.assertEqual(resolve.resolve("work_example.com", self.names, cfg), self.names[0])
        self.assertEqual(resolve.resolve("phone", self.names, cfg), self.names[0])
        self.assertEqual(resolve.resolve("personal@example.com", self.names, cfg), self.names[1])
        self.assertEqual(resolve.resolve("pers", self.names, cfg), self.names[1])

    def test_ambiguous_and_unknown(self):
        with self.assertRaises(SystemExit) as ctx:
            resolve.resolve("example", self.names, {})
        self.assertIn("ambiguous", str(ctx.exception))
        with self.assertRaises(SystemExit) as ctx:
            resolve.resolve("nope", self.names, {})
        self.assertIn("does not exist", str(ctx.exception))

    def test_slug_matches_prodex_names(self):
        self.assertEqual(profiles.slug("Work@Example.com"), "work_example.com")
        self.assertTrue(profiles.valid_name("work_example.com"))
        self.assertFalse(profiles.valid_name("../x"))
        self.assertFalse(profiles.valid_name(".pending-1"))


class QuotaTest(unittest.TestCase):
    def test_plus_is_ready(self):
        q = quota.parse("p", PLUS)
        self.assertEqual(q.status, "ready")
        self.assertEqual([w.name for w in q.windows], ["5h", "weekly"])
        self.assertTrue(q.eligible)
        self.assertEqual(q.reset_credits, 1)
        self.assertEqual(q.remaining, 98)

    def test_free_monthly_window_is_usable(self):
        q = quota.parse("f", FREE)
        self.assertEqual(q.status, "ready")
        self.assertTrue(q.eligible)

    def test_exhausted_window_is_limited_until_its_reset(self):
        data = {**PLUS, "rate_limit": {**PLUS["rate_limit"], "primary_window": {
            "limit_window_seconds": 18000, "used_percent": 100, "reset_at": 111}}}
        q = quota.parse("p", data)
        self.assertEqual(q.status, "limited")
        self.assertEqual(q.usable_again_at, 111)

    def test_roundtrip(self):
        q = quota.parse("p", PLUS)
        self.assertEqual(quota.from_dict(quota.to_dict(q)).windows[1].name, "weekly")


class ClassifyProcTest(unittest.TestCase):
    def proc(self, argv, env=None, pid=1):
        return procs.Proc(pid=pid, ppid=0, argv=argv, env=env or {}, started=0, tty=False)

    def test_roles(self):
        base = "/home/u/.codex/profiles/r/packages/app-server-daemon/releases/x/bin/codex"
        cases = {
            (base, "app-server", "--remote-control", "--listen", "unix://", "--managed-daemon"): "daemon (remote control)",
            (base, "app-server", "--listen", "unix://", "--managed-daemon"): "daemon",
            (base, "app-server", "daemon", "pid-update-loop"): "daemon updater",
            ("/x/bin/codex", "-c", 'openai_base_url="http://127.0.0.1:1/backend-api/codex"', "remote-control"):
                "remote-control server",
            ("/x/bin/codex", "remote-control", "start"): "remote-control start",
            ("/x/bin/codex", "-c", 'openai_base_url="http://127.0.0.1:1/x"', "resume"): "codex resume (rotating)",
            ("/x/bin/codex",): "codex tui",
            ("prodex", "__runtime-broker"): "rotation proxy",
            ("prodex", "run", "--profile", "r", "remote-control"): "prodex remote supervisor",
            ("python3", "/home/u/.local/bin/mycodex", "__remote-serve"): "mycodex remote service",
            ("python3", "/home/u/.local/bin/mycodex", "status"): "",
        }
        for argv, role in cases.items():
            self.assertEqual(procs.classify(self.proc(list(argv))), role, argv)
        self.assertEqual(procs.classify(self.proc(["python3", "/b/mycodex"], pid=7), {7: "session"}),
                         "mycodex session (proxy)")

    def test_profile_from_env_and_path(self):
        p = self.proc(["codex"], {"CODEX_HOME": "/home/u/.codex/profiles/abc_example.com"})
        self.assertEqual(procs._profile_of(p), "abc_example.com")
        p = self.proc(["codex"], {"CODEX_HOME": "/home/u/.prodex/profiles/old"})
        self.assertEqual(procs._profile_of(p), "old")
        p = self.proc(["codex"], {"MYCODEX_PROFILE": "named", "CODEX_HOME": "/elsewhere"})
        self.assertEqual(procs._profile_of(p), "named")
        p = self.proc(["/home/u/.codex/profiles/zz/packages/x/bin/codex"])
        self.assertEqual(procs._profile_of(p), "zz")


class LaunchArgsTest(unittest.TestCase):
    def test_wants_proxy(self):
        for args in ([], ["exec", "hi"], ["resume", "--last"], ["-m", "x"], ["remote-control"], ["app-server"]):
            self.assertTrue(launch.wants_proxy(args), args)
        for args in (["login"], ["logout"], ["mcp", "list"], ["--version"], ["remote-control", "stop"], ["apply", "x"],
                     ["app-server", "proxy", "--sock", "/tmp/s"], ["app-server", "daemon", "status"]):
            self.assertFalse(launch.wants_proxy(args), args)
        self.assertTrue(launch.wants_proxy(["app-server", "--listen", "stdio://"]))

    def test_proxy_args_keep_builtin_provider(self):
        self.assertEqual(launch.proxy_args("http://127.0.0.1:9/backend-api/codex"),
                         ["-c", 'openai_base_url="http://127.0.0.1:9/backend-api/codex"'])

    def test_remote_server_args(self):
        rotating = remote.server_args("rotating", "http://127.0.0.1:9/backend-api/codex")
        self.assertEqual(rotating[-1], "remote-control")
        self.assertIn('openai_base_url="http://127.0.0.1:9/backend-api/codex"', rotating)
        self.assertEqual(remote.server_args("pinned", "http://ignored"), ["remote-control"])

    def test_unit_has_umask_and_restart(self):
        text = remote.unit_text("/home/u/work")
        for needle in ("UMask=0022", "Restart=always", "WorkingDirectory=/home/u/work", "__remote-serve",
                       "KillMode=control-group"):
            self.assertIn(needle, text)


class ErrorPolicyTest(unittest.TestCase):
    def test_http_classification(self):
        usage = json.dumps({"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached",
                                      "plan_type": "plus", "resets_at": 1900000000}}).encode()
        v = proxy.classify(429, usage)
        self.assertEqual((v.kind, v.resets_at, v.rotates), ("quota", 1900000000, True))
        self.assertEqual(proxy.classify(429, b'{"error":{"code":"rate_limit_exceeded"}}').kind, "rate")
        self.assertEqual(proxy.classify(429, b"slow down").kind, "rate")
        self.assertEqual(proxy.classify(429, b'{"error":{"code":"flex_capacity_unavailable"}}').kind, "other")
        self.assertEqual(proxy.classify(403, b'{"detail":{"code":"deactivated_workspace"}}').kind, "profile")
        self.assertEqual(proxy.classify(403, b'{"error":{"message":"You\'ve hit your usage limit."}}').kind, "quota")
        self.assertEqual(proxy.classify(403, b'{"error":{"message":"forbidden"}}').kind, "other")
        self.assertEqual(proxy.classify(503, b'{"error":{"code":"server_is_overloaded"}}').kind, "overload")
        self.assertEqual(proxy.classify(502, b"bad gateway").kind, "transient")
        self.assertFalse(proxy.classify(400, b'{"error":{"message":"bad"}}').rotates)

    def test_stream_scan(self):
        created = b'event: response.created\ndata: {"type":"response.created","response":{}}\n\n'
        self.assertEqual(proxy.scan_events(created), ("wait", None))
        self.assertEqual(proxy.scan_events(created + b'data: {"type":"response.output_item.added"}\n\n')[0], "commit")
        failed = created + b'data: {"type":"response.failed","response":{"error":{"code":"insufficient_quota"}}}\n\n'
        decision, verdict = proxy.scan_events(failed)
        self.assertEqual((decision, verdict.kind), ("fail", "quota"))
        other = created + b'data: {"type":"response.failed","response":{"error":{"code":"context_length_exceeded"}}}\n\n'
        self.assertEqual(proxy.scan_events(other)[0], "commit")
        self.assertEqual(proxy.scan_events(created + b'data: {"type":"response.output_text.delta"'), ("wait", None))


class UiPlainTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"MYCODEX_COLOR": "never", "MYCODEX_WIDTH": "40"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_panel_plain_matches_prodex_layout(self):
        buf = io.StringIO()
        ui.panel("Info", [("Profile", "main"), ("Auth", "chatgpt")], stream=buf)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "[ Info ] " + "=" * (40 - len("[ Info ] ")))
        self.assertEqual(lines[1], "Profile:   main")
        self.assertEqual(lines[2], "Auth:      chatgpt")

    def test_table_plain(self):
        buf = io.StringIO()
        ui.table("T", [ui.Column("A", min_width=1), ui.Column("B", min_width=1)], [["x", "yy"]], stream=buf)
        lines = buf.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("[ T ] "))
        self.assertEqual(lines[1], "A  B")
        self.assertEqual(lines[3], "x  yy")

    def test_fit_and_wrap(self):
        self.assertEqual(ui.fit("abcdef", 4), "abc…")
        self.assertEqual(ui.wrap("aa bb cc", 5), ["aa bb", "cc"])


class ConfigTest(unittest.TestCase):
    def test_defaults_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text('{"version": 1, "remote": {"mode": "pinned"}}')
            data = config.load(path)
            self.assertEqual(data["remote"]["mode"], "pinned")
            self.assertTrue(data["rotation"]["enabled"])
            self.assertIsNone(data["active"])
            self.assertEqual(data["version"], 2)


class MergeTest(unittest.TestCase):
    def test_jsonl_union_ordered_by_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "a.jsonl", Path(tmp) / "b.jsonl"
            dst.write_text('{"id":"1","updated_at":"2026-01-02"}\n')
            src.write_text('{"id":"0","updated_at":"2026-01-01"}\n{"id":"1","updated_at":"2026-01-02"}\n')
            self.assertEqual(profiles.merge_jsonl(src, dst, "updated_at"), 1)
            ids = [json.loads(line)["id"] for line in dst.read_text().splitlines()]
            self.assertEqual(ids, ["0", "1"])


class Sandbox:
    """Temporary shared ~/.codex, profiles root and mycodex state dir."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.shared = root / "codex"
        self.state = root / "state"
        self.shared.mkdir()
        self.patches = [mock.patch.multiple(
            "mycodex.paths", SHARED_CODEX_HOME=self.shared, PROFILES_ROOT=self.shared / "profiles",
            STATE_DIR=self.state, ACCOUNTS_STATE=self.state / "accounts.json", PROXIES_DIR=self.state / "proxies",
            LOCKS_DIR=self.state / "locks", LOG_DIR=self.state / "logs", PROXY_LOG=self.state / "logs" / "proxy.log")]
        for patch in self.patches:
            patch.start()
        from mycodex import state
        state._cache.update(at=0.0, data=None)

    def home(self, name: str) -> Path:
        return self.shared / "profiles" / name

    def close(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()


@unittest.skipUnless(POSIX, "needs fcntl and symlinks")
class HomeTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.close()

    def test_prepare_links_everything_and_reports_private_data(self):
        (self.box.shared / "state_5.sqlite").write_text("")
        home = self.box.home("a")
        home.mkdir(parents=True)
        (home / "sessions").mkdir()
        (home / "sessions" / "keep.jsonl").write_text("x")
        report = profiles.prepare_home(home)
        self.assertEqual(report.private, ["sessions"])
        self.assertEqual(os.readlink(home / "history.jsonl"), str(self.box.shared / "history.jsonl"))
        self.assertEqual(os.readlink(home / "state_5.sqlite-wal"), str(self.box.shared / "state_5.sqlite-wal"))
        self.assertTrue((self.box.shared / "thread-writer-locks").is_dir())
        self.assertTrue((home / ".tmp" / "plugins").is_symlink())
        self.assertEqual(profiles.prepare_home(home, dry_run=True).linked, [])

    def test_adopt_merges_thread_names_and_idle_locks(self):
        home = self.box.home("b")
        (home / "thread-writer-locks").mkdir(parents=True)
        (home / "thread-writer-locks" / "t.lock").write_text("")
        (home / "session_index.jsonl").write_text('{"id":"t","thread_name":"mine","updated_at":"2026-01-01T00:00:00Z"}\n')
        report = profiles.prepare_home(home, adopt=True)
        self.assertEqual(sorted(report.adopted), ["session_index.jsonl", "thread-writer-locks"])
        self.assertIn("mine", (self.box.shared / "session_index.jsonl").read_text())
        self.assertTrue((home / "session_index.jsonl").is_symlink())
        self.assertTrue((home / "thread-writer-locks").is_symlink())

    def test_listing_skips_hidden_dirs(self):
        for name in ("b", "a", ".pending-1", ".removed-x"):
            self.box.home(name).mkdir(parents=True)
        self.assertEqual(profiles.names({"active": "b"}), ["a", "b"])
        self.assertTrue(profiles.get("b", {"active": "b"}).active)


@unittest.skipUnless(POSIX, "needs fcntl")
class AuthTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.home = self.box.home("a")
        write_auth(self.home, "acct-a", "a@example.com")

    def tearDown(self):
        self.box.close()

    def test_claims_never_include_tokens(self):
        from mycodex import auth
        claims = auth.claims(self.home)
        self.assertEqual((claims["email"], claims["plan"], claims["account_id"]), ("a@example.com", "plus", "acct-a"))
        self.assertNotIn("refresh-acct-a", json.dumps(claims))

    def test_refresh_when_about_to_expire_and_persist(self):
        from mycodex import auth
        write_auth(self.home, "acct-a", "a@example.com", expires_in=30)
        fresh = jwt(exp=int(time.time()) + 864000)
        with mock.patch.object(auth, "_exchange", return_value={"access_token": fresh, "refresh_token": "r2"}) as ex:
            creds = auth.credentials(self.home)
        ex.assert_called_once()
        self.assertEqual(creds.access_token, fresh)
        saved = json.loads((self.home / "auth.json").read_text())
        self.assertEqual((saved["tokens"]["refresh_token"], saved["tokens"]["account_id"]), ("r2", "acct-a"))
        self.assertTrue(saved["last_refresh"].endswith("Z"))
        self.assertEqual(oct((self.home / "auth.json").stat().st_mode & 0o777), "0o600")

    def test_refresh_reuses_a_token_another_process_already_renewed(self):
        from mycodex import auth
        with mock.patch.object(auth, "_exchange") as ex:
            creds = auth.refresh(self.home, stale="some-older-token")
        ex.assert_not_called()
        self.assertEqual(creds.account_id, "acct-a")


class FakeBackend(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    behavior: dict = {}
    calls: list = []

    def log_message(self, *args):
        return

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        account = self.headers.get("ChatGPT-Account-Id")
        self.calls.append((account, self.headers.get("Authorization", "")[7:17], self.headers.get("x-codex-turn-state")))
        mode = self.behavior.get(account, "ok")
        if mode == "unauthorized" or (mode == "unauthorized-once" and sum(c[0] == account for c in self.calls) == 1):
            return self.reply(401, b'{"error":{"code":"token_expired"}}', "application/json")
        if mode.startswith("quota"):
            resets = int(mode.split(":")[1]) if ":" in mode else int(time.time()) + 3600
            body = json.dumps({"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached",
                                         "resets_at": resets}}).encode()
            return self.reply(429, body, "application/json")
        created = b'event: response.created\ndata: {"type":"response.created","response":{"id":"r"}}\n\n'
        if mode == "stream-quota":
            failed = b'data: {"type":"response.failed","response":{"error":{"code":"insufficient_quota"}}}\n\n'
            return self.stream([created, failed])
        delta = b'data: {"type":"response.output_text.delta","delta":"hello from %s"}\n\n' % account.encode()
        done = b'data: {"type":"response.completed","response":{"id":"r"}}\n\n'
        return self.stream([created, delta, done], turn_state=f"turn-{account}")

    def reply(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self, events, turn_state=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        if turn_state:
            self.send_header("x-codex-turn-state", turn_state)
        self.end_headers()
        for event in events:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
        self.wfile.write(b"0\r\n\r\n")


@unittest.skipUnless(POSIX, "needs fcntl and /proc")
class ProxyRotationTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        for name in ("a", "b"):
            write_auth(self.box.home(name), f"acct-{name}", f"{name}@example.com")
        cfg = config._merge(config.DEFAULTS, {"active": "a"})
        self.patches = [mock.patch.object(config, "load", return_value=cfg),
                        mock.patch.object(quota, "fetch", side_effect=lambda p, *a, **k: quota.Quota(p.name, False, "unknown"))]
        for patch in self.patches:
            patch.start()
        FakeBackend.calls = []
        FakeBackend.behavior = {}
        self.backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.proxy = proxy.RotationProxy("a", upstream=f"http://127.0.0.1:{self.backend.server_address[1]}").start()

    def tearDown(self):
        self.proxy.stop()
        self.backend.shutdown()
        self.backend.server_close()
        for patch in self.patches:
            patch.stop()
        self.box.close()

    def post(self, session="s1", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        conn.request("POST", "/backend-api/codex/responses", body=b'{"input":[]}',
                     headers={"Content-Type": "application/json", "session-id": session,
                              "Authorization": "Bearer launch-profile-token", **(headers or {})})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def accounts(self):
        return [c[0] for c in FakeBackend.calls]

    def test_usage_limit_moves_the_request_to_the_next_account(self):
        from mycodex import state
        FakeBackend.behavior = {"acct-a": "quota"}
        response, body = self.post()
        self.assertEqual(response.status, 200)
        self.assertIn(b"hello from acct-b", body)
        self.assertEqual(self.accounts(), ["acct-a", "acct-b"])
        self.assertIn("a", state.blocks(max_age=0))
        self.assertEqual(self.proxy.current, "b")
        FakeBackend.calls = []
        self.post()
        self.assertEqual(self.accounts(), ["acct-b"])      # paused account is skipped, session stays on b

    def test_failure_inside_the_stream_before_output_also_rotates(self):
        FakeBackend.behavior = {"acct-a": "stream-quota"}
        response, body = self.post()
        self.assertEqual(response.status, 200)
        self.assertIn(b"hello from acct-b", body)
        self.assertNotIn(b"insufficient_quota", body)

    def test_expired_token_is_refreshed_once(self):
        from mycodex import auth
        FakeBackend.behavior = {"acct-a": "unauthorized-once"}
        fresh = jwt(exp=int(time.time()) + 864000, sub="fresh")
        with mock.patch.object(auth, "_exchange", return_value={"access_token": fresh}):
            response, body = self.post()
        self.assertEqual(response.status, 200)
        self.assertIn(b"hello from acct-a", body)
        self.assertEqual(self.accounts(), ["acct-a", "acct-a"])
        self.assertEqual(json.loads((self.box.home("a") / "auth.json").read_text())["tokens"]["access_token"], fresh)

    def test_all_exhausted_returns_the_earliest_reset(self):
        soon, later = int(time.time()) + 600, int(time.time()) + 7200
        FakeBackend.behavior = {"acct-a": f"quota:{later}", "acct-b": f"quota:{soon}"}
        response, body = self.post()
        self.assertEqual(response.status, 429)
        self.assertEqual(json.loads(body)["error"]["resets_at"], soon)

    def test_turn_state_stays_with_its_issuer(self):
        self.post()
        FakeBackend.calls = []
        FakeBackend.behavior = {"acct-a": "quota"}
        self.post(headers={"x-codex-turn-state": "turn-acct-a"})
        self.assertEqual(self.accounts(), ["acct-a", "acct-b"])
        self.assertEqual(FakeBackend.calls[0][2], "turn-acct-a")
        self.assertIsNone(FakeBackend.calls[1][2])         # another account never sees a's routing token

    def test_completed_response_releases_turn_affinity_but_keeps_token_origin(self):
        self.post()

        self.assertNotIn("turn-acct-a", self.proxy.turns)
        self.assertEqual(self.proxy.issued["turn-acct-a"], "a")

    def test_websocket_upgrade_gets_426(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.port, timeout=10)
        conn.request("GET", "/backend-api/codex/responses", headers={"Upgrade": "websocket", "Connection": "Upgrade"})
        self.assertEqual(conn.getresponse().status, 426)
        conn.close()
        self.assertEqual(FakeBackend.calls, [])

    def test_health_has_no_secrets(self):
        health = proxy.health(self.proxy.port)
        self.assertEqual((health["owner"], health["current"]), ("a", "a"))
        self.assertNotIn("refresh", json.dumps(health))


if __name__ == "__main__":
    unittest.main()
