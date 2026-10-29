"""Coverage for persisted formats seen on the development machine."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agentctl import discover

ID='12345678-1234-1234-1234-123456789abc'
class RealStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)

    def test_codex_response_items_full_text_and_pagination(self):
        p=self.home/'.codex/sessions/2026/10/06'/('rollout-2026-10-06T01-00-00-'+ID+'.jsonl')
        p.parent.mkdir(parents=True)
        events=[{'type':'session_meta','payload':{'id':ID,'cwd':'/workspace'}},
                {'type':'event_msg','payload':{'type':'task_started','turn_id':'turn'}},
                {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'request'}]}},
                {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'x'*5000}]}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn','last_agent_message':'x'*5000}}]
        p.write_text('\n'.join(json.dumps(e) for e in events))
        rows=discover.discover(['codex'],home=str(self.home))
        self.assertEqual(rows[0]['cwd'],'/workspace')
        self.assertEqual(rows[0]['title'],'request')
        history=discover.read_history('codex',ID,home=str(self.home))
        self.assertEqual([m['role'] for m in history],['user','assistant'])
        self.assertEqual(len(history[1]['content']),5000)
        page=discover.read_history('codex',ID,home=str(self.home),limit=1,offset=1)
        self.assertEqual(page[0]['content'],'request')
        self.assertEqual(discover.read_history('codex',ID,home=str(self.home),limit=0),[])

    def test_opencode_local_db_without_server(self):
        p=self.home/'.local/share/opencode/opencode.db';p.parent.mkdir(parents=True)
        c=sqlite3.connect(str(p))
        c.executescript('CREATE TABLE session(id TEXT,title TEXT,directory TEXT,time_updated INTEGER,time_archived INTEGER); CREATE TABLE message(id TEXT,session_id TEXT,time_created INTEGER,data TEXT); CREATE TABLE part(message_id TEXT,time_created INTEGER,id TEXT,data TEXT);')
        c.execute('INSERT INTO session VALUES(?,?,?,?,?)',('ses_local','Local','/workspace',2000,None))
        c.execute('INSERT INTO message VALUES(?,?,?,?)',('msg_a','ses_local',1000,json.dumps({'role':'assistant'})))
        c.execute('INSERT INTO part VALUES(?,?,?,?)',('msg_a',1000,'part_a',json.dumps({'type':'text','text':'result'})))
        c.commit();c.close()
        with mock.patch('agentctl.discover._opencode_sessions_http',return_value=None), mock.patch('agentctl.discover._opencode_history_http',return_value=None):
            rows=discover.discover(['opencode'],home=str(self.home))
            self.assertEqual(rows[0]['native_id'],'ses_local')
            self.assertEqual(rows[0]['modified_at'],2)
            result=discover.read_history('opencode','ses_local',home=str(self.home))
            self.assertEqual(result[0]['content'],'result')

    def test_cursor_native_root_orders_and_excludes_other_branches(self):
        p=self.home/'.cursor/chats/workspace/chat/store.db';p.parent.mkdir(parents=True)
        c=sqlite3.connect(str(p))
        c.executescript('CREATE TABLE blobs(id TEXT PRIMARY KEY,data BLOB); CREATE TABLE meta(key TEXT,value TEXT);')
        first='11'*32;second='22'*32;stale='33'*32;root='44'*32
        # Insertion order differs from the current branch's native message order.
        for key,role,text in [(second,'assistant','reply'),(stale,'assistant','abandoned'),(first,'user','request')]:
            c.execute('INSERT INTO blobs VALUES(?,?)',(key,json.dumps({'role':role,'content':text}).encode()))
        c.execute('INSERT INTO blobs VALUES(?,?)',(root,b'\x0a\x20'+bytes.fromhex(first)+b'\x0a\x20'+bytes.fromhex(second)))
        c.execute('INSERT INTO meta VALUES(?,?)',('0',json.dumps({'latestRootBlobId':root}).encode().hex()))
        c.commit();c.close()
        rows=discover.read_history('cursor','chat',home=str(self.home))
        self.assertEqual([row['content'] for row in rows],['request','reply'])
        self.assertTrue(all(row['ordering']=='native-root' for row in rows))

    def test_cursor_raw_preserves_binary_graph(self):
        import base64
        p=self.home/'.cursor/chats/workspace'/ID/'store.db';p.parent.mkdir(parents=True)
        c=sqlite3.connect(str(p))
        c.executescript('CREATE TABLE blobs(id TEXT PRIMARY KEY,data BLOB); CREATE TABLE meta(key TEXT,value TEXT);')
        binary=b'\x0a\x20'+b'\xff'*32
        c.execute('INSERT INTO blobs VALUES(?,?)',('root',binary));c.commit();c.close()
        records=discover.read_raw_history('cursor',ID,home=str(self.home))
        self.assertEqual(records[0]['encoding'],'base64')
        self.assertEqual(base64.b64decode(records[0]['data']),binary)

    def test_claude_subagent_is_readable_but_not_resumable(self):
        child='abcdefab-1234-1234-1234-abcdefabcdef'
        p=self.home/'.claude/projects/project'/ID/(child+'.jsonl');p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'subagent result'}]}})+'\n')
        row=discover.discover(['claude'],home=str(self.home))[0]
        self.assertFalse(row['capability']['send']);self.assertFalse(row['capability']['resume'])
        self.assertEqual(discover.read_history('claude',child,home=str(self.home))[0]['content'],'subagent result')
