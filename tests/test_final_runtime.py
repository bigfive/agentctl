"""Regression cases from native format and full-runtime integration review."""
import json
import os
import subprocess
import time
from unittest import mock
from tests.test_runtime import RuntimeBase
from agentctl import daemon, sender, sessions, store
from agentctl.errors import InvalidTargetError, DeliveryError

class FinalRuntimeTests(RuntimeBase):
    def wait_result(self, sub):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            row=sessions.read(sub)
            if row.get('result_stored_at') is not None:return row
            time.sleep(.02)
        self.fail('no result observed')

    def test_fast_codex_real_order_is_not_lost(self):
        path=self.make_codex_transcript('thread')
        found=self.make_found_session('codex','thread',path)
        def queue(harness,native,prompt,model=None):
            self.append_transcript(path,[{'type':'event_msg','payload':{'type':'task_started','turn_id':'requested'}},
                {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':prompt}]}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':'requested','last_agent_message':'x'*5000}}])
            return sender.DispatchReceipt(True)
        with mock.patch.object(self.daemon,'_validate_session_exists',return_value=found),mock.patch('agentctl.sender.dispatch_to_session',side_effect=queue):
            receipt=self.daemon.send_session('codex','thread','work')
        row=self.wait_result(receipt['sub'])
        self.assertEqual(row['target_turn_id'],'requested')
        self.assertEqual(row['result'],'x'*5000)
        self.assertEqual(row['callback']['state'],'not_requested')

    def test_native_begin_binds_marker_and_stale_stop_preserves_current(self):
        sub=sessions.create('codex','thread',request_id='req_abcdef')
        self.daemon.notify_hook_event('codex:thread',event='begin',turn_id='wanted',prompt='[agentctl request: req_abcdef]\nwork')
        self.daemon.notify_hook_event('codex:thread',event='complete',turn_id='old',result='unrelated')
        self.assertEqual(sessions.registry_read('codex:thread')['current_turn_id'],'wanted')
        self.assertIsNone(sessions.read(sub['sub'])['result'])
        self.daemon.notify_hook_event('codex:thread',event='complete',turn_id='wanted',result='correct')
        self.assertEqual(sessions.read(sub['sub'])['result'],'correct')

    def test_hook_receiver_is_unsupported_and_result_is_preserved(self):
        sub=sessions.create('codex','thread',from_harness='claude',from_id='receiver')
        sessions.record_result(sub['sub'],'full result')
        self.daemon.deliver_sub_callback(sub['sub'])
        row=sessions.read(sub['sub'])
        self.assertEqual(row['callback']['state'],'unsupported')
        self.assertEqual(row['status'],'stored')
        self.assertEqual(row['result'],'full result')
        self.assertEqual(sessions.inbox_claim_all('claude:receiver'),[])

    def test_callback_crash_claim_is_unknown_and_not_resent(self):
        sub=sessions.create('codex','thread',from_harness='codex',from_id='receiver')
        sessions.record_result(sub['sub'],'full result')
        self.assertTrue(sessions.claim_callback(sub['sub'],'ntf_crashed'))
        with mock.patch('agentctl.notify.get') as get:
            self.daemon.recover()
            self.daemon.deliver_sub_callback(sub['sub'])
        self.assertEqual(sessions.read(sub['sub'])['callback']['state'],'unknown')
        self.assertEqual(sessions.read(sub['sub'])['result'],'full result')
        get.assert_not_called()

    def test_callback_timeout_is_unknown_and_not_resent(self):
        sub=sessions.create('codex','thread',from_harness='codex',from_id='receiver')
        sessions.record_result(sub['sub'],'full result')
        adapter=mock.Mock();adapter.deliver.side_effect=subprocess.TimeoutExpired('native queue',1)
        with mock.patch('agentctl.notify.get',return_value=adapter):
            self.daemon.deliver_sub_callback(sub['sub']);self.daemon.recover()
        self.assertEqual(adapter.deliver.call_count,1)
        self.assertEqual(sessions.read(sub['sub'])['callback']['state'],'unknown')

    def test_cursor_fast_send_uses_one_listener_and_retains_result(self):
        found=self.make_found_session('cursor','native')
        def launch(record,prompt):
            self.assertEqual(len(sessions.list_for_job(record['job'])),1)
            store.write_result(record['job'],'full result')
            store.update(record['job'],status=store.SUCCEEDED)
            self.daemon._deliver_all(record['job'])
        with mock.patch.object(self.daemon,'_validate_session_exists',return_value=found),mock.patch.object(self.daemon,'_launch',side_effect=launch),mock.patch('agentctl.config.resolve_binary',return_value='/fake/cursor'):
            row=self.daemon.send_session('cursor','native','work')
        self.assertEqual(row['result'],'full result')
        self.assertEqual(len(sessions.list_all()),1)
        self.assertEqual(row['callback']['state'],'not_requested')

    def test_manual_expiry_recovers_past_deadline(self):
        sub=sessions.create('codex','thread',timeout=0)
        self.daemon.recover()
        self.assertEqual(sessions.read(sub['sub'])['status'],'expired')

    def test_claude_real_stop_reads_assistant_text_and_missing_turn(self):
        path=os.path.join(self.home,'claude.jsonl')
        with open(path,'w') as f:f.write(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'full native response'}]}})+'\n')
        self.daemon.notify_hook_event('claude:native',event='begin',transcript_path=path)
        sub=self.daemon.register_session_sub('claude','native')
        self.daemon.notify_hook_event('claude:native',event='complete',transcript_path=path)
        self.assertEqual(sessions.read(sub['sub'])['result'],'full native response')

    def test_require_unverified_codex_rejects_before_dispatch(self):
        with mock.patch('agentctl.notify.codex.CodexNotify.preflight',return_value={'verified':False,'state':'unverified','note':'control route unavailable'}),mock.patch('agentctl.sender.dispatch_to_session') as dispatch:
            with self.assertRaises(InvalidTargetError):
                self.daemon.send_session('codex','thread','work',subscriber_harness='codex',subscriber_id='12345678-1234-1234-1234-123456789abc',require_callback=True)
        dispatch.assert_not_called();self.assertEqual(sessions.list_all(),[])

    def test_opencode_busy_uses_native_status_endpoint(self):
        with mock.patch('agentctl.httpc.request',return_value=(200,{'ses_native':{'type':'busy'}})) as request:
            with self.assertRaises(DeliveryError):self.daemon._check_opencode_session_busy('ses_native')
        self.assertTrue(request.call_args.args[1].endswith('/session/status'))

    def test_partial_jsonl_tail_is_not_skipped(self):
        path=self.make_codex_transcript('thread')
        cursor=os.path.getsize(path)
        with open(path,'ab') as f:f.write(b'{"type":"event_msg"')
        new,events=daemon._read_transcript_from(path,cursor)
        self.assertEqual(new,cursor);self.assertEqual(events,[])
        with open(path,'ab') as f:f.write(b',"payload":{"type":"task_started","turn_id":"turn"}}\n')
        new,events=daemon._read_transcript_from(path,new)
        self.assertEqual(events[0]['payload']['turn_id'],'turn')

    def test_expiry_keeps_already_stored_result(self):
        sub=sessions.create('codex','thread',timeout=0)
        sessions.record_result(sub['sub'],'retained result')
        self.daemon._expire_sub(sub['sub'])
        self.assertEqual(sessions.read(sub['sub'])['result'],'retained result')

    def test_classic_callback_claim_recovers_without_resend(self):
        job=store.create('claude','work',self.home,command=[])
        store.write_result(job['job'],'result')
        store.update(job['job'],status=store.SUCCEEDED)
        wait=store.add_wait(job['job'],'codex','receiver','codex:receiver',None)
        store.set_delivery(job['job'],wait['wait'],store.DELIVERY_INFLIGHT,notification_id='ntf_claimed')
        with mock.patch('agentctl.notify.get') as get:self.daemon.recover()
        self.assertEqual(store.read(job['job'])['waits'][0]['delivery']['state'],'unknown')
        get.assert_not_called()

    def test_resume_unaddressable_kind_rejects_before_launch(self):
        from agentctl.errors import UnsupportedOperationError
        found=self.make_found_session('claude','subagent');found['capability']={'resume':False,'resume_reason':'subagent is not resumable'}
        with mock.patch.object(self.daemon,'_validate_session_exists',return_value=found),mock.patch.object(self.daemon,'_launch') as launch:
            with self.assertRaises(UnsupportedOperationError):self.daemon.resume_external('claude','subagent','work',self.home)
        launch.assert_not_called();self.assertEqual(store.list_jobs(),[])
