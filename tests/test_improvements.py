"""Regression coverage for project workflows, quota policy, and narrow database repair."""
import contextlib
import io
import json
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

import test_core as core
from test_core import Sandbox, FakeBackend, write_auth
from mycodex import appserver, cli, config, launch, maintenance, model_cache, profiles, projects, proxy, quota, redemption, remote_cmd, state, threads

TID = '00000000-0000-4000-8000-000000000001'
PROJECT = {'id': 'p1', 'name': 'Example', 'roots': [{'path': '/work/example'}]}


class RPC:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def result(self, method, params=None, **kwargs):
        self.calls.append((method, params or {}))
        value = self.responses.get(method, {})
        if callable(value):
            return value(params or {})
        if isinstance(value, Exception):
            raise value
        return value


class ProjectWorkflowTest(unittest.TestCase):
    def test_second_page_project_is_reused_without_creating_a_thread(self):
        server = RPC({'project/list': lambda p: {'data': [] if not p.get('cursor') else [PROJECT],
                                                  'nextCursor': 'next' if not p.get('cursor') else None}})
        project, created = projects.ensure(server, Path('/work/example'))
        self.assertEqual(project, PROJECT)
        self.assertFalse(created)
        self.assertEqual([m for m, _ in server.calls], ['project/list', 'project/list'])

    def test_project_create_is_idempotent_and_has_no_seed(self):
        server = RPC({'project/list': {'data': []}, 'project/create': {'project': PROJECT}})
        projects.ensure(server, Path('/work/example'), 'Example')
        first = server.calls[-1]
        projects.ensure(server, Path('/work/example'), 'Example')
        self.assertEqual(server.calls[-1], first)
        self.assertEqual(first[1]['roots'], [{'path': '/work/example'}])
        self.assertNotIn('thread/start', [m for m, _ in server.calls])

    def test_duplicate_project_names_are_not_guessed(self):
        other = {**PROJECT, 'id': 'p2'}
        with self.assertRaisesRegex(appserver.AppServerError, 'ambiguous'):
            projects.resolve([PROJECT, other], 'Example')
        self.assertEqual(projects.resolve([PROJECT, other], 'p1'), PROJECT)

    def test_implicit_directory_uses_deepest_project_root(self):
        parent = {'id': 'parent', 'name': 'Parent', 'roots': [{'path': '/work'}]}
        self.assertEqual(projects.resolve([parent, PROJECT], cwd='/work/example/src'), PROJECT)

    def test_repeated_pagination_cursor_fails_instead_of_looping(self):
        with self.assertRaises(appserver.AppServerError):
            projects.pages(RPC({'project/list': {'data': [], 'nextCursor': 'again'}}), 'project/list')

    def test_link_assigns_then_hydrates_same_thread_without_starting_turn(self):
        server = RPC({'project/list': {'data': [PROJECT]},
                      'thread/resume': {'thread': {'id': TID, 'projectId': 'p1', 'status': {'type': 'idle'}}}})
        row = {'id': TID, 'model_provider': 'openai', 'cwd': '/work/example'}
        with mock.patch.object(threads, '_find', return_value=row), mock.patch.object(projects, 'connection', return_value=server), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(threads.link(TID, 'Example'), 0)
        self.assertEqual(server.calls[-2:], [('thread/metadata/update', {'threadId': TID, 'projectId': 'p1'}),
                                           ('thread/resume', {'threadId': TID, 'excludeTurns': True})])
        self.assertNotIn('turn/start', [m for m, _ in server.calls])

    def test_resume_must_confirm_loaded_status_not_only_project_assignment(self):
        server = RPC({'project/list': {'data': [PROJECT]},
                      'thread/resume': {'thread': {'id': TID, 'projectId': 'p1', 'status': {'type': 'notLoaded'}}}})
        with mock.patch.object(threads, '_find', return_value={'id': TID, 'cwd': '/work/example'}), mock.patch.object(projects, 'connection', return_value=server), self.assertRaises(appserver.AppServerError):
            threads.link(TID, 'Example')

    def test_hydration_failure_reports_assignment_saved(self):
        server = RPC({'project/list': {'data': [PROJECT]}, 'thread/resume': appserver.AppServerError('busy')})
        with mock.patch.object(threads, '_find', return_value={'id': TID, 'cwd': '/work/example'}), mock.patch.object(projects, 'connection', return_value=server), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(threads.link(TID, 'Example'), 1)
        self.assertIn('assignment saved', err.getvalue())

    def test_counts_request_all_sources_and_providers(self):
        server = RPC({'thread/list': {'data': [{'id': TID}]}})
        self.assertEqual(len(projects.member_threads(server, 'p1')), 1)
        params = server.calls[-1][1]
        self.assertEqual(params['modelProviders'], [])
        self.assertIn('exec', params['sourceKinds'])
        self.assertFalse(params['archived'])

    def test_cleanup_preserves_real_work_and_running_threads(self):
        dummy = {'status': {'type': 'idle'}, 'turns': [{'status': 'completed', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': projects.READY_MESSAGE}]},
            {'type': 'agentMessage', 'text': 'ready'}]}]}
        self.assertTrue(projects.disposable(dummy))
        self.assertFalse(projects.disposable({**dummy, 'status': {'type': 'active'}}))
        self.assertFalse(projects.disposable({**dummy, 'turns': dummy['turns'] * 2}))
        dummy['turns'][0]['items'].append({'type': 'commandExecution', 'command': 'ls'})
        self.assertFalse(projects.disposable(dummy))
        self.assertFalse(projects.disposable({'turns': [], 'preview': 'Real task'}))

    def test_cleanup_rechecks_thread_before_archiving(self):
        reads = iter([{'id': TID, 'projectId': 'p1', 'turns': [], 'preview': '', 'status': {'type': 'idle'}},
                      {'id': TID, 'projectId': 'p1', 'turns': [], 'preview': '', 'status': {'type': 'active'}}])
        server = RPC({'project/list': {'data': [PROJECT]},
                      'thread/list': lambda p: {'data': [] if 'ancestorThreadId' in p else [{'id': TID}]},
                      'thread/read': lambda p: {'thread': next(reads)}})
        with mock.patch.object(projects, 'connection', return_value=server), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(projects.clean('Example', assume_yes=True), 0)
        self.assertNotIn('thread/archive', [m for m, _ in server.calls])


class NotificationTest(unittest.TestCase):
    def test_wait_matches_thread_and_turn_and_preserves_unrelated_completion(self):
        server = appserver.AppServer('/unused')
        other = {'method': 'turn/completed', 'params': {'threadId': 'other', 'turn': {'id': 'other-turn'}}}
        wanted = {'method': 'turn/completed', 'params': {'threadId': TID, 'turn': {'id': 'turn1'}}}
        server._receive = mock.Mock(side_effect=[other, wanted])
        result = server.wait_for('turn/completed', predicate=lambda n: n['params']['threadId'] == TID and n['params']['turn']['id'] == 'turn1')
        self.assertEqual(result, wanted)
        self.assertEqual(server.notifications, [other])
        self.assertEqual(server.wait_for('turn/completed'), other)
        self.assertEqual(server.notifications, [])

    def test_late_reset_lookup_does_not_recreate_a_cleared_pause(self):
        box = Sandbox()
        try:
            until = int(time.time()) + 300
            state.block('a', until, 'quota: exhausted')
            state.clear_quota_pause('a')
            state.extend_pause('a', until, until + 3600)
            self.assertNotIn('a', state.blocks(max_age=0))
        finally:
            box.close()

    def test_connection_failure_closes_its_child(self):
        server = appserver.AppServer('/unused')
        server.open = mock.Mock(side_effect=appserver.AppServerError('handshake failed'))
        server.close = mock.Mock()
        with self.assertRaises(appserver.AppServerError):
            with server:
                pass
        server.close.assert_called_once()


class RoutingTest(unittest.TestCase):
    def test_uuid_shortcut_preserves_profile_and_extra_flags(self):
        with mock.patch('mycodex.paths.migrate_legacy', return_value=[]), mock.patch.object(cli, 'launch', return_value=0) as run:
            self.assertEqual(cli.main(['--profile', 'work', TID, '--last']), 0)
        self.assertEqual(run.call_args.args[0].profile, 'work')
        self.assertEqual(run.call_args.args[1], ['resume', TID, '--last'])

    def test_native_codex_commands_still_pass_through(self):
        with mock.patch('mycodex.paths.migrate_legacy', return_value=[]), mock.patch.object(cli, 'launch', return_value=0) as run:
            cli.main(['exec', 'hello'])
        self.assertEqual(run.call_args.args[1], ['exec', 'hello'])

    def test_top_level_use_alias(self):
        with mock.patch('mycodex.paths.migrate_legacy', return_value=[]), mock.patch('mycodex.accounts.profile_use', return_value=0) as use:
            cli.main(['use', 'work'])
        use.assert_called_once_with('work')

    def test_redeem_profile_flag_routes_to_the_requested_account(self):
        with mock.patch('mycodex.paths.migrate_legacy', return_value=[]), mock.patch.object(redemption, 'redeem', return_value=0) as redeem:
            cli.main(['--profile', 'work', 'quota', 'redeem', '--json'])
        self.assertEqual(redeem.call_args.args[:2], ('work', True))

    def test_launch_dry_run_prepares_home_without_writing_links(self):
        from mycodex.profiles import Profile
        with mock.patch('mycodex.profiles.prepare_home') as prepare, contextlib.redirect_stdout(io.StringIO()):
            launch.run(Profile('work', Path('/unused')), [], True, dry_run=True)
        prepare.assert_called_once_with(Path('/unused'), dry_run=True)

    def test_failover_default_respects_existing_explicit_off(self):
        self.assertTrue(config.DEFAULTS['remote']['failover'])
        self.assertFalse(config._merge(config.DEFAULTS, {'remote': {'failover': False}})['remote']['failover'])


class HeadroomTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        for name in ['a', 'b']:
            write_auth(self.box.home(name), 'acct-' + name, name + '@example.com')
        self.cfg = config._merge(config.DEFAULTS, {'active': 'a'})
        self.mock = mock.patch.object(config, 'load', return_value=self.cfg)
        self.mock.start()
        self.proxy = proxy.RotationProxy('a')

    def tearDown(self):
        self.proxy.server.server_close()
        self.mock.stop()
        self.box.close()

    def remember(self, name, remaining, seconds=18000, age=0):
        q = quota.Quota(name, True, 'ready', windows=[quota.Window('5h', seconds, 100-remaining, int(time.time())+3600)], fetched_at=time.time()-age, usage_allowed=True)
        state.save_quota(name, quota.to_dict(q))

    def test_fresh_turn_moves_off_low_account_but_sticky_turn_stays(self):
        self.remember('a', 2); self.remember('b', 100)
        self.assertEqual([p.name for p in self.proxy.plan(None, 's')], ['b', 'a'])
        self.proxy.turns['sticky'] = ('a', time.time())
        self.assertEqual([p.name for p in self.proxy.plan('sticky', 's')], ['a', 'b'])

    def test_confirmed_zero_5h_quota_is_routed_before_a_backend_rejection(self):
        self.remember('a', 0); self.remember('b', 100)
        snapshot = quota.cached('a'); snapshot.status = 'limited'
        state.save_quota('a', quota.to_dict(snapshot))
        self.assertEqual(self.proxy.plan(None, None)[0].name, 'b')

    def test_opt_in_reset_precedes_proactive_rotation_at_zero(self):
        self.remember('a', 0); self.remember('b', 100)
        self.cfg['rotation']['auto_redeem'] = True
        def recover(profile):
            self.remember(profile.name, 100)
            return True
        with mock.patch.object(redemption, 'try_auto', side_effect=recover) as redeem:
            self.assertEqual(self.proxy.plan(None, None)[0].name, 'a')
        redeem.assert_called_once()

    def test_non_model_requests_do_not_trigger_proactive_routing(self):
        self.remember('a', 2); self.remember('b', 100)
        self.assertEqual(self.proxy.plan(None, None, fresh_turn=False)[0].name, 'a')

    def test_no_healthy_alternative_keeps_the_current_account(self):
        self.remember('a', 2); self.remember('b', 3)
        self.assertEqual(self.proxy.plan(None, None)[0].name, 'a')

    def test_threshold_disabled_and_exact_boundary_do_not_switch(self):
        self.remember('a', 5); self.remember('b', 100)
        self.assertEqual(self.proxy.plan(None, None)[0].name, 'a')
        self.cfg['rotation']['min_quota_headroom'] = 0
        self.remember('a', 2)
        self.assertEqual(self.proxy.plan(None, None)[0].name, 'a')

    def test_stale_snapshot_is_refreshed_before_routing(self):
        self.remember('a', 2, age=1000); self.remember('b', 100)
        with mock.patch.object(quota, 'fetch_all', side_effect=lambda ps, **kw: self.remember('a', 80)) as fetch:
            self.assertEqual(self.proxy.plan(None, None)[0].name, 'a')
        self.assertEqual([p.name for p in fetch.call_args.args[0]], ['a'])

    def test_no_5h_window_is_not_misclassified_as_low_5h(self):
        self.remember('a', 2, seconds=604800); self.remember('b', 100)
        self.assertEqual(self.proxy.plan(None, None)[0].name, 'a')


class QuotaPriorityRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        for name in ('a', 'b'):
            write_auth(self.box.home(name), 'acct-' + name, name + '@example.com')
        self.cfg = config._merge(config.DEFAULTS, {'active': 'a', 'rotation': {
            'order': ['a', 'b'], 'min_quota_headroom': 0,
        }})
        self.config = mock.patch.object(config, 'load', return_value=self.cfg)
        self.config.start()
        self.proxy = proxy.RotationProxy('a')

    def tearDown(self):
        self.proxy.server.server_close()
        self.config.stop()
        self.box.close()

    @staticmethod
    def usage(name, status, windows):
        return quota.Quota(name, status != 'unknown', status, windows=windows,
                           usage_allowed=True if status == 'ready' else False)

    def save_limited(self, *windows):
        state.save_quota('a', quota.to_dict(self.usage('a', 'limited', list(windows))))

    def test_due_five_hour_reset_is_refreshed_before_selecting_a_fresh_prompt(self):
        now = int(time.time())
        self.save_limited(quota.Window('5h', 18000, 100, now - 1))
        state.block('a', now - 1, 'quota: exhausted')
        ready = self.usage('a', 'ready', [quota.Window('5h', 18000, 0, now + 18000)])

        with mock.patch.object(quota, 'fetch', return_value=ready) as fetch:
            result = self.proxy.plan(None, 'session')

        self.assertEqual([p.name for p in result], ['a', 'b'])
        fetch.assert_called_once()
        self.assertNotIn('a', state.quota_pauses())

    def test_due_weekly_and_multiple_exhausted_windows_wait_for_latest_reset(self):
        now = int(time.time())
        self.save_limited(quota.Window('5h', 18000, 100, now - 1),
                          quota.Window('weekly', 604800, 100, now - 1))
        state.block('a', now - 1, 'quota: exhausted')
        still_limited = self.usage('a', 'limited', [
            quota.Window('5h', 18000, 100, now + 18000),
            quota.Window('weekly', 604800, 100, now + 604800),
        ])

        with mock.patch.object(quota, 'fetch', return_value=still_limited):
            result = self.proxy.plan(None, 'session')

        self.assertEqual(result[0].name, 'b')
        self.assertEqual(state.quota_pauses()['a'][0], now + 604800)

    def test_failed_due_recheck_does_not_promote_or_clear_quota_pause(self):
        now = int(time.time())
        self.save_limited(quota.Window('5h', 18000, 100, now - 1))
        state.block('a', now - 1, 'quota: exhausted')

        with mock.patch.object(quota, 'fetch', return_value=self.usage('a', 'unknown', [])):
            result = self.proxy.plan(None, 'session')

        self.assertEqual(result[0].name, 'b')
        self.assertIn('a', state.quota_pauses())
        self.assertIn('a', state.blocks(max_age=0))

    def test_fresh_prompt_reorders_same_session_after_completed_response(self):
        now = time.time()
        self.proxy.current = 'b'
        self.proxy.sessions['session'] = ('b', now)
        self.proxy.turns['turn'] = ('b', now)
        self.proxy.issued['turn'] = 'b'

        self.proxy.finish_turn('turn', None)

        self.assertEqual(self.proxy.plan(None, 'session')[0].name, 'a')
        self.assertNotIn('turn', self.proxy.turns)
        self.assertEqual(self.proxy.issued['turn'], 'b')

    def test_in_progress_turn_state_keeps_account_affinity(self):
        self.proxy.turns['turn'] = ('b', time.time())

        self.assertEqual(self.proxy.plan('turn', 'session')[0].name, 'b')

    def test_non_quota_pause_is_not_cleared_by_quota_recheck(self):
        state.block('a', time.time() + 300, 'auth: invalid')

        with mock.patch.object(quota, 'fetch') as fetch:
            self.proxy.plan(None, 'session')

        self.assertIn('a', state.blocks(max_age=0))
        fetch.assert_not_called()


class RedemptionTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        write_auth(self.box.home('a'), 'acct-a', 'a@example.com')
        from mycodex.profiles import Profile
        self.profile = Profile('a', self.box.home('a'))

    def tearDown(self):
        self.box.close()

    def server(self, outcome='reset', permission=True):
        return RPC({'account/rateLimitResetCredit/consume': {'outcome': outcome},
                    'account/rateLimits/read': {'accountId': 'acct-a', 'ordinaryUsageAllowed': permission,
                        'rateLimits': {'primary': {'usedPercent': 0, 'windowDurationMins': 300, 'resetsAt': int(time.time())+3600}},
                        'rateLimitResetCredits': {'availableCount': 0}}})

    def test_success_reads_authoritative_limits_and_clears_only_quota_pause(self):
        state.block('a', time.time()+3600, 'quota: exhausted')
        server = self.server()
        with mock.patch.object(redemption, 'AccountServer', return_value=server):
            result = redemption.consume(self.profile)
        self.assertEqual(result['outcome'], 'reset')
        self.assertNotIn('a', state.blocks(max_age=0))
        self.assertEqual([m for m, _ in server.calls], ['account/rateLimitResetCredit/consume', 'account/rateLimits/read'])
        state.block('a', time.time()+3600, 'auth: invalid')
        with mock.patch.object(redemption, 'AccountServer', return_value=self.server()):
            redemption.consume(self.profile)
        self.assertIn('a', state.blocks(max_age=0))

    def test_ambiguous_failure_reuses_persisted_idempotency_key(self):
        server = self.server()
        server.responses['account/rateLimitResetCredit/consume'] = appserver.AppServerError('connection lost')
        with mock.patch.object(redemption, 'AccountServer', return_value=server), self.assertRaises(appserver.AppServerError):
            redemption.consume(self.profile)
        key = server.calls[0][1]['idempotencyKey']
        retry = self.server(outcome='alreadyRedeemed')
        with mock.patch.object(redemption, 'AccountServer', return_value=retry):
            redemption.consume(self.profile)
        self.assertEqual(retry.calls[0][1]['idempotencyKey'], key)

    def test_unknown_permission_does_not_clear_pause_based_on_percentages(self):
        state.block('a', time.time()+3600, 'quota: exhausted')
        with mock.patch.object(redemption, 'AccountServer', return_value=self.server(permission=None)):
            redemption.consume(self.profile)
        self.assertIn('a', state.blocks(max_age=0))

    def test_snapshot_account_mismatch_does_not_clear_pause(self):
        state.block('a', time.time()+3600, 'quota: exhausted')
        server = self.server()
        server.responses['account/rateLimits/read']['accountId'] = 'another-account'
        with mock.patch.object(redemption, 'AccountServer', return_value=server), self.assertRaises(appserver.AppServerError):
            redemption.consume(self.profile)
        self.assertIn('a', state.blocks(max_age=0))

    def test_no_credit_outcome_does_not_claim_recovery(self):
        state.block('a', time.time()+3600, 'quota: exhausted')
        with mock.patch.object(redemption, 'AccountServer', return_value=self.server(outcome='noCredit')):
            result = redemption.consume(self.profile)
        self.assertEqual(result['outcome'], 'noCredit')
        self.assertIn('a', state.blocks(max_age=0))

    def test_auto_redeem_preserves_credits_when_other_windows_are_exhausted(self):
        q = quota.Quota('a', True, 'limited', reset_credits=1, windows=[quota.Window('5h', 18000, 100, 1), quota.Window('weekly', 604800, 100, 2)])
        with mock.patch.object(quota, 'fetch', return_value=q), mock.patch.object(redemption, 'consume') as consume:
            self.assertFalse(redemption.try_auto(self.profile))
        consume.assert_not_called()


class AutoRedemptionProxyTest(unittest.TestCase):
    setUp = core.ProxyRotationTest.setUp
    tearDown = core.ProxyRotationTest.tearDown
    post = core.ProxyRotationTest.post
    accounts = core.ProxyRotationTest.accounts

    def test_opt_in_redemption_retries_same_account_before_rotating(self):
        self.proxy.pool()[1]['rotation']['auto_redeem'] = True
        FakeBackend.behavior = {'acct-a': 'quota'}
        def recover(profile):
            FakeBackend.behavior = {}
            state.clear_quota_pause(profile.name)
            return True
        with mock.patch.object(redemption, 'try_auto', side_effect=recover) as redeem:
            response, body = self.post()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.accounts(), ['acct-a', 'acct-a'])
        redeem.assert_called_once()

    def test_opt_in_failure_falls_back_to_rotation(self):
        self.proxy.pool()[1]['rotation']['auto_redeem'] = True
        FakeBackend.behavior = {'acct-a': 'quota'}
        with mock.patch.object(redemption, 'try_auto', side_effect=appserver.AppServerError('unsupported')):
            response, body = self.post()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.accounts(), ['acct-a', 'acct-b'])


class RolloutRepairTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.db = self.box.shared / 'state_5.sqlite'
        self.old = '/old/.prodex/profiles/a/sessions/2026/01/01/rollout-test-' + TID + '.jsonl'
        with sqlite3.connect(self.db) as conn:
            conn.execute('create table threads(id text primary key, rollout_path text, title text)')
            conn.execute('insert into threads values(?,?,?)', (TID, self.old, 'Keep my history'))
        self.target = self.box.shared / 'sessions/2026/01/01' / Path(self.old).name
        self.target.parent.mkdir(parents=True)
        self.target.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': TID}}) + '\n')

    def tearDown(self):
        self.box.close()

    def row(self, db=None):
        with sqlite3.connect(db or self.db) as conn:
            return conn.execute('select rollout_path,title from threads').fetchone()

    def test_audit_is_read_only_and_fix_backs_up_before_changing_only_path(self):
        self.assertEqual(maintenance.audit()[0]['repairable'], 1)
        self.assertEqual(self.row()[0], self.old)
        result = maintenance.audit(fix=True)[0]
        self.assertEqual(result['repaired'], 1)
        self.assertEqual(self.row(), (str(self.target), 'Keep my history'))
        self.assertEqual(self.row(result['backup']), (self.old, 'Keep my history'))
        self.assertEqual(maintenance.audit(fix=True)[0]['legacy'], 0)

    def test_missing_or_wrong_identity_rollout_is_preserved_and_reported(self):
        self.target.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'wrong'}}))
        result = maintenance.audit(fix=True)[0]
        self.assertEqual(result['repaired'], 0)
        self.assertEqual(len(result['unresolved']), 1)
        self.assertEqual(self.row()[0], self.old)
        self.assertIsNone(result['backup'])

    def test_profile_database_symlinks_are_repaired_only_once(self):
        home = self.box.home('a'); home.mkdir(parents=True)
        (home / 'state_5.sqlite').symlink_to(self.db)
        self.assertEqual(len(maintenance.audit(fix=True)), 1)

    def test_ambiguous_duplicate_rollouts_are_not_guessed(self):
        duplicate = self.box.shared / 'archived_sessions' / self.target.name
        duplicate.parent.mkdir()
        duplicate.write_text(self.target.read_text())
        result = maintenance.audit(fix=True)[0]
        self.assertEqual(result['repaired'], 0)
        self.assertEqual(self.row()[0], self.old)

    def test_active_writer_lock_prevents_repair(self):
        import fcntl
        path = self.box.shared / 'thread-writer-locks' / (TID + '.lock')
        path.parent.mkdir()
        with path.open('wb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = maintenance.audit(fix=True)[0]
        self.assertEqual(result['repaired'], 0)
        self.assertEqual(self.row()[0], self.old)
        self.assertIn('busy', result['unresolved'][0]['reason'])


class RemoteDefaultTest(unittest.TestCase):
    def start_with_existing_off(self, restart):
        box = Sandbox()
        try:
            write_auth(box.home('a'), 'acct-a', 'a@example.com')
            cfg = config._merge(config.DEFAULTS, {'active': 'a', 'remote': {'profile': 'a', 'mode': 'rotating', 'cwd': str(box.shared), 'failover': False}})
            @contextlib.contextmanager
            def edit():
                yield cfg
            with mock.patch.object(config, 'load', return_value=cfg), mock.patch.object(config, 'editing', side_effect=edit), mock.patch.object(quota, 'fetch', return_value=quota.Quota('a', True, 'ready')), mock.patch('mycodex.procs.scan', return_value=[]), mock.patch('mycodex.remote.service_info', return_value={'ActiveState':'active','MainPID':'0'}), mock.patch('mycodex.remote.read_serve_state', return_value={'profile':'a','mode':'rotating'}), mock.patch('mycodex.remote.install_unit', return_value=False), mock.patch('mycodex.remote._systemctl'), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(remote_cmd.start(wait=False, restart=restart), 0)
            return cfg['remote']['failover']
        finally:
            box.close()

    def test_new_start_defaults_to_failover_even_with_old_saved_default(self):
        self.assertTrue(self.start_with_existing_off(False))

    def test_restart_preserves_explicit_off(self):
        self.assertFalse(self.start_with_existing_off(True))


class ModelCacheShareTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.source_home = self.box.home('plus')
        self.target_home = self.box.home('relay')
        self.source_home.mkdir(parents=True)
        self.target_home.mkdir(parents=True)
        self.source = profiles.Profile('plus', self.source_home)
        self.target = profiles.Profile('relay', self.target_home)
        self.cfg = config._merge(config.DEFAULTS, {'active': 'plus', 'remote': {'profile': 'relay'}})

    def tearDown(self):
        self.box.close()

    @contextlib.contextmanager
    def editing(self):
        yield self.cfg

    def test_model_cache_share_copies_only_valid_json_to_relay_atomically(self):
        source_cache = self.source_home / 'models_cache.json'
        target_cache = self.target_home / 'models_cache.json'
        source_cache.write_text('{"models":["plus"]}\n')
        target_cache.write_text('{"models":["old"]}\n')
        (self.target_home / 'auth.json').write_text('{"account":"relay"}\n')
        (self.target_home / 'installation_id').write_text('relay-identity\n')

        self.assertTrue(model_cache.sync(self.source, self.target))

        self.assertEqual(target_cache.read_text(), '{"models":["plus"]}\n')
        self.assertEqual(source_cache.read_text(), '{"models":["plus"]}\n')
        self.assertEqual((self.target_home / 'auth.json').read_text(), '{"account":"relay"}\n')
        self.assertEqual((self.target_home / 'installation_id').read_text(), 'relay-identity\n')
        self.assertEqual(target_cache.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.target_home.glob('.models_cache.json.*')), [])

    def test_model_cache_share_defaults_active_profile_and_saves_source(self):
        (self.source_home / 'models_cache.json').write_text('{"models":["plus"]}')
        with mock.patch.object(config, 'load', return_value=self.cfg), mock.patch.object(config, 'editing', self.editing), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(remote_cmd.share_models(None), 0)

        self.assertEqual((self.target_home / 'models_cache.json').read_text(), '{"models":["plus"]}')
        self.assertEqual(self.cfg['remote']['models_source'], 'plus')

    def test_model_cache_share_explicit_source_overrides_active_profile(self):
        other_home = self.box.home('other')
        other_home.mkdir()
        (self.source_home / 'models_cache.json').write_text('{"models":["active"]}')
        (other_home / 'models_cache.json').write_text('{"models":["explicit"]}')
        with mock.patch.object(config, 'load', return_value=self.cfg), mock.patch.object(config, 'editing', self.editing), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(remote_cmd.share_models('other'), 0)

        self.assertEqual((self.target_home / 'models_cache.json').read_text(), '{"models":["explicit"]}')
        self.assertEqual(self.cfg['remote']['models_source'], 'other')

    def test_model_cache_share_failure_preserves_target_and_settings(self):
        target_cache = self.target_home / 'models_cache.json'
        target_cache.write_text('{"models":["old"]}')
        (self.source_home / 'models_cache.json').write_text('{not-json')
        self.cfg['remote']['models_source'] = 'other'

        with mock.patch.object(config, 'load', return_value=self.cfg), mock.patch.object(config, 'editing', self.editing), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(remote_cmd.share_models(None), 1)

        self.assertEqual(target_cache.read_text(), '{"models":["old"]}')
        self.assertEqual(self.cfg['remote']['models_source'], 'other')

    def test_model_cache_share_rejects_target_symlink_escape(self):
        outside = self.box.shared / 'outside.json'
        outside.write_text('{"models":["outside"]}')
        (self.source_home / 'models_cache.json').write_text('{"models":["plus"]}')
        (self.target_home / 'models_cache.json').symlink_to(outside)

        with self.assertRaises(model_cache.ModelCacheError):
            model_cache.sync(self.source, self.target)

        self.assertEqual(outside.read_text(), '{"models":["outside"]}')

    def test_model_cache_share_rejects_source_symlink_to_profile_data(self):
        target_cache = self.target_home / 'models_cache.json'
        target_cache.write_text('{"models":["old"]}')
        unrelated = self.source_home / 'auth.json'
        unrelated.write_text('{"unrelated":true}')
        (self.source_home / 'models_cache.json').symlink_to(unrelated)

        with self.assertRaises(model_cache.ModelCacheError):
            model_cache.sync(self.source, self.target)

        self.assertEqual(target_cache.read_text(), '{"models":["old"]}')

    def test_model_cache_same_source_and_target_is_a_noop(self):
        cache = self.source_home / 'models_cache.json'
        cache.write_text('{"models":["same"]}')

        self.assertFalse(model_cache.sync(self.source, self.source))

        self.assertEqual(cache.read_text(), '{"models":["same"]}')
