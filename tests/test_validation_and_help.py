"""Boundary and CLI regressions, using only isolated files and mocked actions."""
import contextlib
import copy
import io
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from test_core import PLUS, Sandbox, jwt, write_auth
from test_improvements import RPC
from mycodex import accounts, appserver, auth, cli, config, helptext, paths, profiles, quota, redemption, state, validation


class QuotaValidationTest(unittest.TestCase):
    def test_empty_responses_never_prove_readiness(self):
        for payload in ({}, {'rate_limit': None}, {'rate_limit': {}}, {'rate_limit': {'allowed': None}}):
            with self.subTest(payload=payload):
                q = quota.parse('work', payload)
                self.assertEqual(q.status, 'unknown')
                self.assertFalse(q.eligible)
                self.assertIsNotNone(q.error)

    def test_healthy_window_without_permission_is_unknown(self):
        payload = copy.deepcopy(PLUS)
        del payload['rate_limit']['allowed']
        self.assertFalse(quota.parse('work', payload).eligible)

    def test_permission_can_be_ready_without_optional_windows(self):
        self.assertTrue(quota.parse('work', {'rate_limit': {'allowed': True}}).eligible)

    def test_explicit_denial_and_exhaustion_win_over_readiness(self):
        for rate in ({'allowed': False}, {'allowed': True, 'limit_reached': True}):
            self.assertEqual(quota.parse('work', {'rate_limit': rate}).status, 'limited')
        payload = copy.deepcopy(PLUS)
        payload['rate_limit']['primary_window']['used_percent'] = 120
        self.assertEqual(quota.parse('work', payload).status, 'limited')

    def test_invalid_container_shapes_are_rejected(self):
        for payload in ([], 'secret-input', None, {'rate_limit': []}, {'rate_limit_reset_credits': []}):
            with self.subTest(payload=payload), self.assertRaises(validation.InvalidData):
                quota.parse('work', payload)

    def test_invalid_window_numbers_and_permissions_are_rejected(self):
        invalid = [None, 'secret-input', True, -1, float('nan'), float('inf'), 10 ** 1000]
        for field in ('used_percent', 'limit_window_seconds', 'reset_at'):
            for value in invalid:
                if field == 'reset_at' and value is None:
                    continue
                payload = copy.deepcopy(PLUS)
                payload['rate_limit']['primary_window'][field] = value
                with self.subTest(field=field, value=type(value).__name__), self.assertRaises(validation.InvalidData) as ctx:
                    quota.parse('work', payload)
                self.assertNotIn('secret-input', str(ctx.exception))
        for value in (0, 1, 'true', [], {}):
            with self.subTest(permission=value), self.assertRaises(validation.InvalidData):
                quota.parse('work', {'rate_limit': {'allowed': value}})

    def test_missing_required_window_measurement_is_not_zero_usage(self):
        for window in ({}, {'limit_window_seconds': 18000}, {'used_percent': 0}):
            with self.assertRaises(validation.InvalidData):
                quota.parse('work', {'rate_limit': {'allowed': True, 'primary_window': window}})

    def test_reset_credits_are_nonnegative_whole_numbers(self):
        for count in (-1, 0.5, True, '1', float('inf')):
            with self.assertRaises(validation.InvalidData):
                quota.parse('work', {'rate_limit_reset_credits': {'available_count': count}})

    def test_rpc_readiness_requires_explicit_permission(self):
        rate = {'primary': {'usedPercent': 1, 'windowDurationMins': 300, 'resetsAt': 100}}
        for permission in (None, False, True):
            q = quota.from_rpc('work', {'rateLimits': rate, 'ordinaryUsageAllowed': permission})
            self.assertEqual(q.eligible, permission is True)
        for payload in ([], {'rateLimits': []}, {'rateLimitsByLimitId': []}, {'ordinaryUsageAllowed': 'true'}):
            with self.assertRaises(validation.InvalidData):
                quota.from_rpc('work', payload)

    def test_saved_legacy_readiness_is_rechecked(self):
        data = quota.to_dict(quota.parse('work', PLUS))
        del data['usage_allowed']
        self.assertFalse(quota.from_dict(data).eligible)
        self.assertTrue(quota.from_dict(quota.to_dict(quota.parse('work', PLUS))).eligible)

    def test_failed_checks_replace_stale_ready_cache(self):
        box = Sandbox()
        try:
            p = profiles.Profile('work', box.home('work'))
            state.save_quota('work', quota.to_dict(quota.parse('work', PLUS)))
            with mock.patch.object(auth, 'credentials', return_value=auth.Credentials('hidden', 'a', None)), mock.patch.object(quota, '_get', return_value=(200, b'[]')):
                q = quota.fetch(p)
            self.assertFalse(q.eligible)
            self.assertFalse(quota.cached('work').eligible)
            self.assertNotIn('hidden', q.error)
        finally:
            box.close()

    def test_bad_reply_does_not_crash_multi_account_check(self):
        with mock.patch.object(auth, 'credentials', return_value=auth.Credentials('hidden', 'a', None)), mock.patch.object(quota, '_get', return_value=(200, b'null')), mock.patch.object(state, 'save_quota'):
            result = quota.fetch_all([profiles.Profile(n, Path('/unused')) for n in ('a', 'b')])
        self.assertEqual(set(result), {'a', 'b'})
        self.assertTrue(all(q.status == 'unknown' for q in result.values()))


class FileValidationTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()

    def tearDown(self):
        self.box.close()

    def test_malformed_login_facts_are_safe_to_display(self):
        home = self.box.home('work'); home.mkdir(parents=True)
        for data in ([], {'tokens': [1]}, {'tokens': {'access_token': 12}}, {'tokens': {'access_token': 'hidden\nheader'}}, {'tokens': {'id_token': jwt(email=['hidden'])}}, {'tokens': {'id_token': jwt(**{'https://api.openai.com/auth': ['hidden']})}}):
            (home / 'auth.json').write_text(json.dumps(data))
            self.assertTrue(auth.claims(home)['corrupt'])
            with self.assertRaises(auth.AuthError) as ctx:
                auth.credentials(home)
            self.assertNotIn('hidden', str(ctx.exception))
        pending = self.box.shared / '.replacement-login'
        write_auth(pending, 'a', 'work@example.com')
        cfg = config._merge(config.DEFAULTS, {'active': 'work'})
        with mock.patch.object(config, 'load', return_value=cfg), mock.patch.object(profiles, 'new_pending', return_value=pending), mock.patch.object(accounts, '_codex_login', return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(accounts.profile_reauth('work', False), 0)
        self.assertEqual(auth.claims(home)['email'], 'work@example.com')

    def test_invalid_expiry_yields_login_error(self):
        home = self.box.home('work'); home.mkdir(parents=True)
        for exp in ('hidden', float('nan'), float('inf'), True, -1):
            (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': jwt(exp=exp)}}))
            with self.assertRaises(auth.AuthError):
                auth.credentials(home)

    def test_malformed_refresh_never_overwrites_login(self):
        home = self.box.home('work'); write_auth(home, 'a', 'work@example.com')
        before = (home / 'auth.json').read_bytes()
        for payload in ([], {}, {'access_token': []}, {'access_token': 'hidden', 'refresh_token': 2}, {'access_token': jwt(exp='hidden')}):
            with mock.patch.object(auth, '_exchange', return_value=payload), self.assertRaises(auth.AuthError):
                auth.refresh(home)
            self.assertEqual((home / 'auth.json').read_bytes(), before)

    def test_bad_refresh_error_code_is_not_dereferenced(self):
        self.assertIsNone(auth._error_code(b'{"error":{"code":[]}}'))
        self.assertEqual(auth._error_code(b'{"error":{"code":"invalid_grant"}}'), 'invalid_grant')

    def test_settings_shapes_and_numbers_are_validated_without_writes(self):
        p = self.box.shared / 'settings.json'
        cases = [[], {'active': []}, {'rotation': []}, {'remote': []}, {'aliases': []},
                 {'rotation': {'order': 'hidden'}}, {'rotation': {'disabled': [12]}},
                 {'rotation': {'enabled': 'false'}}, {'rotation': {'auto_redeem': 1}},
                 {'rotation': {'min_quota_headroom': float('nan')}},
                 {'remote': {'mode': 'hidden'}}, {'remote': {'failover': 'false'}},
                 {'remote': {'failover_check_seconds': 0}}, {'remote': {'cwd': []}},
                 {'aliases': {'work': ['hidden']}}]
        for data in cases:
            p.write_text(json.dumps(data)); before = p.read_bytes()
            with self.subTest(data=data), self.assertRaises(SystemExit) as ctx:
                config.load(p)
            self.assertNotIn('hidden', str(ctx.exception))
            self.assertEqual(p.read_bytes(), before)
        for raw in ('{"a":' + '1' * 5000 + '}', '[' * 1100 + ']' * 1100):
            p.write_text(raw)
            with self.assertRaises(SystemExit):
                config.load(p)
            self.assertEqual(p.read_text(), raw)

    def test_unknown_settings_are_preserved(self):
        p = self.box.shared / 'settings.json'
        p.write_text('{"future_setting":{"a":1}}')
        self.assertEqual(config.load(p)['future_setting'], {'a': 1})

    def test_damaged_durable_state_is_not_replaced(self):
        paths.state_dir()
        for text in ('{', '{"a":' + '1' * 5000 + '}', '[' * 1100 + ']' * 1100, '[]', '{"profiles":[]}', '{"profiles":{"work":{"reset_attempt":[]}}}', '{"profiles":{"work":{"blocked_until":"hidden"}}}'):
            paths.ACCOUNTS_STATE.write_text(text)
            with self.assertRaises(state.StateError):
                state.reset_attempt('work')
            self.assertEqual(paths.ACCOUNTS_STATE.read_text(), text)

    def test_bad_usage_cache_does_not_discard_pending_reset(self):
        paths.state_dir()
        paths.ACCOUNTS_STATE.write_text('{"profiles":{"work":{"reset_attempt":"retry-key","quota":{"fetched_at":"bad"}}}}')
        self.assertIsNone(quota.cached('work'))
        self.assertEqual(state.reset_attempt('work'), 'retry-key')

    def test_malformed_reset_reply_keeps_retry_identity_and_pause(self):
        home = self.box.home('work'); write_auth(home, 'a', 'work@example.com')
        state.block('work', int(time.time()) + 300, 'quota: exhausted')
        server = RPC({'account/rateLimitResetCredit/consume': []})
        with mock.patch.object(redemption, 'AccountServer', return_value=server), self.assertRaises(validation.InvalidData):
            redemption.consume(profiles.Profile('work', home))
        key = state.reset_attempt('work')
        self.assertEqual(server.calls[0][1]['idempotencyKey'], key)
        self.assertIn('work', state.blocks(max_age=0))

    def test_cli_entry_reports_damaged_file_without_traceback(self):
        with mock.patch.object(cli, 'main', side_effect=state.StateError('Keep the history file')), contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as ctx:
            cli.entry()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn('Keep the history file', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())


class ProtocolValidationTest(unittest.TestCase):
    def test_bad_websocket_messages_produce_readable_errors(self):
        for payload in (b'[]', b'null', b'bad-json', b'\xff'):
            server = appserver.AppServer('/unused')
            server._buf = bytes([0x81, len(payload)]) + payload
            with self.assertRaises(appserver.AppServerError):
                server._receive(time.time() + 1)

    def test_bad_rpc_error_shape_is_handled(self):
        server = appserver.AppServer('/unused')
        server.call = mock.Mock(return_value={'error': ['hidden']})
        with self.assertRaises(appserver.AppServerError) as ctx:
            server.result('account/rateLimits/read')
        self.assertNotIn('hidden', str(ctx.exception))


class FriendlyCLITest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch('mycodex.paths.migrate_legacy', return_value=[])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_every_subcommand_has_help_without_required_values(self):
        for name in helptext.DETAILS:
            with self.subTest(name=name), contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as ctx:
                cli.main([*name.split(), '--help'])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn('Usage:', out.getvalue())
            for flag, _ in helptext.DETAILS[name][2]:
                self.assertIn(flag.split(',')[-1].split()[0], out.getvalue())

    def test_nested_help_and_shortcut_help(self):
        for words in (['help', 'quota', 'redeem'], ['help', 'use'], ['help', 'ps'], ['help', 'profiles']):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(words), 0)
            self.assertIn('Usage:', out.getvalue())

    def test_quota_account_selector_and_all_override(self):
        cases = [(['--profile', 'work', 'quota'], 'work'),
                 (['--profile', 'work', 'quota', 'home'], 'home'),
                 (['--profile', 'work', 'quota', '--all'], None)]
        for words, expected in cases:
            with mock.patch.object(accounts, 'quota_cmd', return_value=0) as run:
                cli.main(words)
            self.assertEqual(run.call_args.args[0], expected)

    def test_global_preview_reaches_cleanup_and_migration(self):
        with mock.patch('mycodex.projects.clean', return_value=0) as clean:
            cli.main(['--dry-run', 'projects', 'clean', 'Example', '--yes'])
        clean.assert_called_once_with('Example', True, True)
        with mock.patch('mycodex.migrate.run', return_value=0) as run:
            cli.main(['--dry-run', 'migrate'])
        run.assert_called_once_with(True, False, True)

    def test_unsupported_preview_never_runs_command(self):
        with mock.patch.object(accounts, 'profile_remove') as remove, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--dry-run', 'profile', 'remove', 'work', '--yes']), 2)
        remove.assert_not_called()

    def test_foreground_rejects_ignored_service_options(self):
        for words in (['--cwd', '/work'], ['--failover'], ['--no-failover'], ['--force'], ['--no-wait']):
            with mock.patch('mycodex.remote_cmd.foreground') as run, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                cli.main(['remote-control', '--foreground', *words])
            self.assertEqual(ctx.exception.code, 2)
            run.assert_not_called()

    def test_invalid_numbers_are_rejected_before_actions(self):
        commands = [('quota', '--watch'), ('rotation', 'log', '-n'), ('remote', 'logs', '-n'), ('threads', 'list', '-n'), ('rotation', 'headroom')]
        for words in commands:
            for value in ('0', '-1', 'nan', 'inf', 'word'):
                if words[-1] == 'headroom' and value == '0':
                    continue
                with self.subTest(words=words, value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                    cli.main([*words, value])
                self.assertEqual(ctx.exception.code, 2)

    def test_missing_global_account_value_has_clear_error(self):
        for args in (['--profile'], ['--profile', '--help'], ['--profile=']):
            with self.assertRaises(SystemExit) as ctx:
                cli.parse_globals(args)
            self.assertIn('Choose an account', str(ctx.exception))

    def test_full_reference_uses_the_terminal_help_descriptions(self):
        text = helptext.markdown()
        for name, (description, _, _) in helptext.DETAILS.items():
            self.assertIn('## ' + name, text)
            self.assertIn(description, text)

    def test_remote_models_share_help_and_generated_guide_match(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as ctx:
            cli.main(['remote', 'models', 'share', '--help'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn('mycodex remote models share [SOURCE]', out.getvalue())
        description = helptext.DETAILS['remote models share'][0]
        self.assertIn(description, out.getvalue())
        self.assertIn('## remote models share', helptext.markdown())
        self.assertIn(description, helptext.markdown())
