"""Deterministic transport-fault and HTTP safety checks; no device is contacted."""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen
import numpy as np

import app
from wifipresence.core import DataError, Config
from dataclasses import asdict
from wifipresence.presence_model import FEATURE_NAMES, FEATURE_VERSION
from wifipresence.presence_live import LiveSession

# Artificial model fixture for transport behavior only, never written to a product model file.
TEST_MODEL = {'kind':'experimental_presence_logistic','feature_version':FEATURE_VERSION,'feature_names':FEATURE_NAMES,
 'synthetic':False,'confidence':.8,'center':[0.0]*20,'scale':[1.0]*20,'weights':[0.0]*20,'bias':4.0,
 'profile':{'device_id':'TEST_DEVICE','firmware_version':'3.0.0','requested_pps':100.0,'room':'418'},
 'config':asdict(Config()),'fit_sha256':['0'*64],'fit_trial_ids':['SOFTWARE_FIXTURE_ONLY']}

class LiveChecks(unittest.TestCase):
    def packet(self,i):
        return SimpleNamespace(iq_raw=np.full(128,10,np.int8),device_ticks_us=i*10000,
            phy_mode='ht',ltf_type='ht-ltf',channel_width='20',
            device_id=TEST_MODEL['profile']['device_id'],firmware_version='3.0.0')

    def populated(self,model=TEST_MODEL,clock=None):
        session=LiveSession(model=model,clock=clock or time.monotonic)
        session.status='STREAMING'
        for i in range(180):session.accept_packet(self.packet(i))
        return session

    def test_stale_stopped_no_model_and_bad_window_are_unknown(self):
        clock=[0.0];s=self.populated(clock=lambda:clock[0])
        self.assertEqual(s.snapshot()['status'],'STREAMING')
        clock[0]=4.0
        self.assertEqual((s.snapshot()['presence'],s.snapshot()['reason']),('UNKNOWN','stale_stream'))
        s.status='STOPPED';self.assertEqual(s.snapshot()['presence'],'UNKNOWN')
        missing=self.populated(model=None)
        self.assertEqual((missing.snapshot()['presence'],missing.snapshot()['reason']),('UNKNOWN','model_not_ready'))
        poor=LiveSession(model=TEST_MODEL);poor.status='STREAMING'
        for i in (0,40,80,120,160):poor.accept_packet(self.packet(i))
        self.assertEqual(poor.snapshot()['presence'],'UNKNOWN')
        self.assertEqual(poor.snapshot()['reason'],'insufficient_window_quality')

    def test_backpressure_and_room_mismatch_are_unknown(self):
        s=self.populated();s.transport_problem=True
        self.assertEqual(s.snapshot()['reason'],'raw_transport_drops_or_backpressure')
        s=self.populated();s.room='other-room'
        self.assertEqual(s.snapshot()['reason'],'room_scope_not_confirmed')
        self.assertEqual(s.snapshot()['presence'],'UNKNOWN')

    def test_empty_reference_retries_quality_once_within_session_time(self):
        for recovery, deadline in [(True, 120), (False, 120), (False, 50), (True, None)]:
            with self.subTest(recovery=recovery, deadline=deadline):
                clock = [24.]
                session = LiveSession(model=TEST_MODEL, clock=lambda: clock[0], calibrated_live=True)
                session.status = 'STREAMING'
                session.stream_deadline = deadline
                session.reference_state = 'collecting'
                for i in range(0, 2401, 2):
                    session.accept_packet(self.packet(i))
                first = session.snapshot()
                ref = first['diagnostics']['empty_reference']
                self.assertEqual(ref['quality']['valid_windows'], 0)
                self.assertEqual(ref['quality']['total_windows'], 93)
                self.assertEqual(ref['quality']['median_slot_coverage'], .5)
                self.assertEqual(ref['quality']['minimum_slot_coverage'], .7)
                self.assertEqual(first['presence'], 'UNKNOWN')
                self.assertIsNone(session.empty_reference)
                self.assertEqual(session.reference_samples, [])
                if deadline == 50:
                    self.assertEqual(ref['state'], 'insufficient')
                    continue
                self.assertEqual(ref['state'], 'collecting')
                self.assertEqual(ref['mode'], 'fresh_after_quality_retry')
                self.assertEqual(ref['elapsed_s'], 0)

                clock[0] = 49.
                step = 1 if recovery else 2
                for i in range(2402, 4803, step):
                    session.accept_packet(self.packet(i))
                second = session.snapshot()
                ref = second['diagnostics']['empty_reference']
                self.assertEqual(ref['state'], 'ready' if recovery else 'insufficient')
                self.assertEqual(second['presence'], 'EMPTY' if recovery else 'UNKNOWN')
                self.assertEqual(ref['quality']['valid_windows'], 93 if recovery else 0)
                self.assertEqual(session.reference_samples, [])

    def test_confirmed_empty_reference_controls_live_decisions_and_expires(self):
        session = LiveSession(model=TEST_MODEL, calibrated_live=True)
        session.status = 'STREAMING'
        for i in range(180):
            session.accept_packet(self.packet(i))
        self.assertEqual(session.snapshot()['reason'], 'empty_reference_required')
        session.reset()
        session.status = 'STREAMING'
        session.reference_state = 'collecting'
        for i in range(2401):
            session.accept_packet(self.packet(i))
            if i == 179:
                self.assertEqual(session.snapshot()['reason'], 'empty_reference_collecting')
        empty = session.snapshot()
        self.assertEqual((empty['presence'], empty['reason']), ('EMPTY', 'empty_reference_match'))
        self.assertEqual(empty['diagnostics']['empty_reference']['state'], 'ready')
        self.assertIsNone(empty['windows'][-1]['model_score'])
        self.assertEqual(session.reference_samples, [])
        for i in range(2401, 2720):
            packet = self.packet(i)
            packet.iq_raw[:] = 30
            session.accept_packet(packet)
        changed = session.snapshot()
        self.assertEqual((changed['presence'], changed['reason']), ('PRESENT', 'empty_reference_deviation'))
        for i in range(180):
            session.accept_packet(self.packet(i))
        invalidated = session.snapshot()
        self.assertEqual((invalidated['presence'], invalidated['reason']), ('UNKNOWN', 'empty_reference_invalidated'))

    def test_sustained_presence_requires_nonoverlapping_valid_windows(self):
        from wifipresence.empty_reference import confirm_sustained_presence

        strong = {'start_s': 0.0, 'end_s': 1.0, 'reason': 'empty_reference_deviation'}
        overlap = {'start_s': 0.25, 'end_s': 1.25, 'reason': 'empty_reference_deviation'}
        current = {'start_s': 1.0, 'end_s': 2.0, 'reason': 'empty_reference_uncertain',
                   'reference_distance': 1.6, 'presence': 'UNKNOWN'}
        confirm_sustained_presence([overlap, current])
        self.assertEqual(current['presence'], 'UNKNOWN')
        confirm_sustained_presence([strong, overlap, current])
        self.assertEqual((current['presence'], current['reason']), ('PRESENT', 'empty_reference_sustained'))
        rejection = {**current, 'reference_distance': None, 'reason': 'insufficient_window_quality',
                     'presence': 'UNKNOWN'}
        confirm_sustained_presence([strong, rejection])
        self.assertEqual(rejection['presence'], 'UNKNOWN')
        empty = {**current, 'reason': 'empty_reference_match', 'reference_distance': 0.85, 'presence': 'EMPTY'}
        confirm_sustained_presence([strong, empty])
        self.assertEqual(empty['presence'], 'EMPTY')

    def test_saved_reference_short_check_and_automatic_full_fallback(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from wifipresence.reference_store import ReferenceStore

        with TemporaryDirectory() as directory:
            store = ReferenceStore(Path(directory) / 'reference.json')
            session = LiveSession(model=TEST_MODEL, calibrated_live=True, reference_store=store)
            addr = '192.168.1.50'
            def start(reuse):
                with patch.object(session, '_run', side_effect=lambda *_: setattr(session, 'status', 'STREAMING')):
                    session.start(addr, None, 60, empty_confirmed=True, reuse_reference=reuse)
                    session.thread.join(1)
            def packets(start, end, amplitude):
                for i in range(start, end):
                    p = self.packet(i)
                    p.iq_raw[:] = amplitude
                    session.accept_packet(p)

            start(True)
            self.assertEqual(session.snapshot()['diagnostics']['empty_reference']['required_s'], 24)
            packets(0, 2401, 10)
            fresh = session.snapshot()
            self.assertEqual(fresh['presence'], 'EMPTY')
            self.assertTrue(fresh['diagnostics']['empty_reference']['saved'])
            self.assertTrue(store.path.is_file())
            self.assertIsNone(store.load({**session.reference_key, 'target': '192.168.1.51'}))

            start(True)
            self.assertEqual(session.snapshot()['diagnostics']['empty_reference']['required_s'], 5)
            packets(0, 501, 10)
            reused = session.snapshot()
            self.assertEqual(reused['presence'], 'EMPTY')
            self.assertEqual(reused['diagnostics']['empty_reference']['mode'], 'reused')
            self.assertEqual(session.reference_samples, [])

            start(True)
            packets(0, 501, 30)
            mismatch = session.snapshot()
            self.assertEqual(mismatch['presence'], 'UNKNOWN')
            self.assertEqual(mismatch['diagnostics']['empty_reference']['mode'], 'fresh_after_mismatch')
            self.assertEqual(mismatch['diagnostics']['empty_reference']['required_s'], 24)
            packets(501, 2401, 30)
            recalibrated = session.snapshot()
            self.assertEqual(recalibrated['presence'], 'EMPTY')
            self.assertTrue(recalibrated['diagnostics']['empty_reference']['saved'])

    def test_diagnostics_trace_low_rate_and_transport(self):
        low=LiveSession(model=TEST_MODEL,room='418');low.status='STREAMING'
        step=1_000_000/64
        for i in range(200):
            p=self.packet(i);p.device_ticks_us=int(i*step);low.accept_packet(p)
        snap=low.snapshot();d=snap['diagnostics']
        self.assertEqual(snap['reason'],'insufficient_window_quality')
        self.assertLess(d['last_window']['occupancy'],0.7)
        self.assertAlmostEqual(d['observed_pps'],64,delta=3)
        self.assertEqual(d['reason_counts'],{'insufficient_window_quality':1})
        bp=self.populated();p=self.packet(180);p.raw_drop_total=7;p.raw_send_backpressure_total=2
        bp.accept_packet(p)
        snap=bp.snapshot();d=snap['diagnostics']
        self.assertEqual(snap['reason'],'raw_transport_drops_or_backpressure')
        self.assertEqual((d['transport']['drops'],d['transport']['backpressure']),(7,2))
        self.assertEqual(d['transport']['first_seen_packet'],181)

    def test_result_computed_during_stop_cannot_be_reused(self):
        s=self.populated();entered=threading.Event();release=threading.Event();out=[]
        def slow_infer(*args,**kwargs):
            entered.set();release.wait(2)
            return [{'presence':'EMPTY','reason':'experimental_model_output'}]
        with patch('wifipresence.presence_live.infer',slow_infer):
            t=threading.Thread(target=lambda:out.append(s.snapshot()));t.start()
            self.assertTrue(entered.wait(2))
            with s.lock:s.status='STOPPING';s.stop_event.set()
            release.set();t.join(3)
        self.assertEqual(out[0]['presence'],'UNKNOWN')
        self.assertEqual(out[0]['reason'],'stream_changed_during_processing')

    def test_simulated_start_stop_closes_and_restores_original_mode(self):
        state={'mode':'internal','closed':False,'patches':[]};ready=threading.Event();owner=self
        class Client:
            def __init__(self,url,**kwargs):self.url=url
            def request(self,op,path,data=None):
                if path=='device':return {'chip':'esp32c6','firmware':'3.0.0'}
                if path=='capabilities':return {'csi':{'protocol_version':1,'marker':'TEST'}}
                if path=='diagnostics':return {'raw_csi':{'active':False}}
                if path=='sensing' and op=='patch':state['mode']=data['traffic_generator_mode'];state['patches'].append(state['mode']);return {}
                return {'traffic_generator_mode':state['mode']}
            def close(self):state['closed']=True
        class Receiver:
            def __init__(self,*args,**kwargs):self.running=True
            def add_callback(self,callback):self.callback=callback
            def start_session(self):pass
            def bind_stream(self):pass
            def run(self,**kwargs):
                for i in range(180):self.callback(owner.packet(i))
                ready.set()
                while self.running:time.sleep(.01)
            def stop(self):self.running=False
        class Generator:
            TRAFFIC_MARKER='TEST'
            def __init__(self,*args,**kwargs):pass
            def start(self):pass
            def stop(self):pass
        s=LiveSession(model=TEST_MODEL);s.start('192.168.1.50',None,12,(Client,Receiver,Generator))
        self.assertTrue(ready.wait(5));live=s.snapshot();self.assertEqual(live['status'],'STREAMING')
        self.assertEqual(live['presence'],'PRESENT');self.assertEqual(live['reason'],'experimental_model_output')
        with self.assertRaises(DataError):s.start('192.168.1.50',None,12,(Client,Receiver,Generator))
        s.stop();end=s.snapshot()
        self.assertEqual(end['status'],'STOPPED');self.assertEqual(end['presence'],'UNKNOWN')
        self.assertTrue(end['session_closed']);self.assertTrue(end['restored'])
        self.assertEqual(state['mode'],'internal');self.assertEqual(state['patches'],['external','internal']);self.assertTrue(state['closed'])

    def test_device_backpressure_reconnect_is_bounded(self):
        for scenario, expected_status, attempts in [
            ('recovered', 'STOPPED', 1),
            ('continuous', 'STOPPED', 1),
            ('fails_again', 'ERROR', 1),
            ('device_restarted', 'STOPPED', 1),
            ('no_reference', 'ERROR', 0),
            ('cleanup_failed', 'ERROR', 0),
            ('not_enough_time', 'ERROR', 0),
            ('user_stopped', 'ERROR', 0),
            ('another_session', 'ERROR', 1),
        ]:
            with self.subTest(scenario=scenario):
                self.check_device_backpressure(scenario, expected_status, attempts)

    def check_device_backpressure(self, scenario, expected_status, attempts):
        now = [0.0]
        session = LiveSession(clock=lambda: now[0], calibrated_live=True)
        timeouts = []
        reopened = []
        state = {'mode': 'internal', 'failures': 0, 'running': False}
        reference = SimpleNamespace(window_count=10)
        packet = self.packet

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, op, path, data=None):
                if path == 'device':
                    return {'chip': 'esp32c6', 'firmware': '3.0.0'}
                if path == 'capabilities':
                    return {'csi': {'protocol_version': 1, 'marker': 'TEST'}}
                if path == 'sensing':
                    if op == 'patch':
                        if scenario == 'cleanup_failed' and data['traffic_generator_mode'] == 'internal':
                            raise OSError('restore failed')
                        state['mode'] = data['traffic_generator_mode']
                    return {'traffic_generator_mode': state['mode']}
                active = state['running'] or (scenario == 'another_session' and bool(reopened))
                failures = state['failures']
                return {'raw_csi': {'active': active, 'raw_drop_total': 102 * failures,
                                    'send_backpressure_total': int(failures > 0)}}

            def close(self):
                pass

        class Receiver:
            def __init__(self, *args, **kwargs):
                self.running = True

            def add_callback(self, callback):
                self.callback = callback

            def start_session(self):
                state['running'] = True
                state['failures'] = 0

            def bind_stream(self):
                pass

            def run(self, *, timeout, quiet):
                timeouts.append(timeout)
                if len(timeouts) == 1:
                    if scenario != 'no_reference':
                        session.empty_reference = reference
                        session.reference_state = 'ready'
                    for idx in range(180):
                        self.callback(packet(idx))
                    now[0] += 86 if scenario == 'not_enough_time' else 45
                    if scenario == 'user_stopped':
                        session.stop_event.set()
                else:
                    self.callback(packet(0 if scenario == 'device_restarted' else 180))
                    if scenario != 'fails_again':
                        return
                state['running'] = False
                state['failures'] = 1
                raise RuntimeError(
                    'Direct raw HTTP session ended on the device (send backpressure: 1, dropped records: 102)'
                )

            def stop(self):
                state['running'] = False

        class Generator:
            TRAFFIC_MARKER = 'TEST'

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        def during_reconnect(delay):
            current = session.snapshot()
            reopened.append(current)
            now[0] += delay
            return False

        with patch.object(session.stop_event, 'wait', side_effect=during_reconnect):
            duration = None if scenario == 'continuous' else 90
            session._run('192.168.1.50', None, duration, (Client, Receiver, Generator))
        result = session.snapshot()
        diag = result['diagnostics']
        self.assertEqual((result['status'], result['presence']), (expected_status, 'UNKNOWN'))
        self.assertEqual(diag['reconnect_attempts'], attempts)
        failure = diag['last_transport_failure']
        self.assertEqual((failure['drops'], failure['backpressure'], failure['first_seen_packet']), (102, 1, None))
        self.assertEqual(state['mode'], 'external' if scenario == 'cleanup_failed' else 'internal')
        if attempts:
            self.assertEqual((reopened[0]['status'], reopened[0]['presence'], reopened[0]['reason']),
                             ('RECONNECTING', 'UNKNOWN', 'raw_transport_reconnecting'))
            self.assertTrue(reopened[0]['session_closed'])
            self.assertTrue(reopened[0]['restored'])
            if scenario == 'another_session':
                self.assertEqual(timeouts, [90])
                self.assertFalse(result['session_closed'])
                return
            self.assertEqual(timeouts, [0, 0] if scenario == 'continuous' else [90, 44.5])
            self.assertEqual(result['packets'], 181)
            self.assertEqual(len(session.buffer), 1)
            self.assertIs(session.empty_reference, None if scenario == 'device_restarted' else reference)
            self.assertEqual(diag['transport']['backpressure'], 1 if scenario == 'fails_again' else 0)
        else:
            self.assertEqual(timeouts, [90])
            self.assertEqual(result['packets'], 180)
            self.assertEqual(diag['transport']['backpressure'], 1)

    def test_existing_raw_session_is_never_taken_over(self):
        state={'patches':0,'closed':False}
        class Client:
            def __init__(self,*args,**kwargs):pass
            def request(self,op,path,data=None):
                if op=='patch':state['patches']+=1
                if path=='device':return {'chip':'esp32c6','firmware':'3.0.0'}
                if path=='sensing':return {'traffic_generator_mode':'external'}
                if path=='diagnostics':return {'raw_csi':{'active':True}}
                return {}
            def close(self):state['closed']=True
        class Unused:
            TRAFFIC_MARKER='TEST'
            def __init__(self,*args,**kwargs):raise AssertionError('Must not create receiver or generator')
        s=LiveSession(model=TEST_MODEL);s._run('192.168.1.50',None,12,(Client,Unused,Unused))
        self.assertEqual(s.snapshot()['status'],'ERROR');self.assertEqual(s.snapshot()['presence'],'UNKNOWN')
        self.assertIn('Another raw CSI session',s.error);self.assertEqual(state['patches'],0)
        self.assertIsNone(s.restored);self.assertFalse(s.session_closed);self.assertTrue(state['closed'])

    def test_existing_external_mode_is_not_reapplied(self):
        state={'patches':[]}
        class Client:
            def __init__(self,*args,**kwargs):pass
            def request(self,op,path,data=None):
                if op=='patch':state['patches'].append(data);return {}
                if path=='device':return {'chip':'esp32c6','firmware':'3.0.0'}
                if path=='sensing':return {'traffic_generator_mode':'external'}
                if path=='diagnostics':return {'raw_csi':{'active':False}}
                if path=='capabilities':return {'csi':{'protocol_version':1,'marker':'TEST'}}
            def close(self):pass
        class Receiver:
            def __init__(self,*args,**kwargs):pass
            def add_callback(self,callback):pass
            def start_session(self):pass
            def bind_stream(self):pass
            def run(self,**kwargs):pass
            def stop(self):pass
        class Generator:
            TRAFFIC_MARKER='TEST'
            def __init__(self,*args,**kwargs):pass
            def start(self):pass
            def stop(self):pass
        s=LiveSession(model=TEST_MODEL);s._run('192.168.1.50',None,12,(Client,Receiver,Generator))
        self.assertEqual(state['patches'],[]);self.assertEqual(s.status,'STOPPED')
        self.assertTrue(s.restored);self.assertTrue(s.session_closed)

    def test_http_simulated_stream_with_software_model_fixture(self):
        state={'mode':'internal'};ready=threading.Event();owner=self
        now = [0.0]
        class Client:
            def __init__(self,*args,**kwargs):pass
            def request(self,op,path,data=None):
                if path=='device':return {'chip':'esp32c6','firmware':'3.0.0'}
                if path=='capabilities':return {'csi':{'protocol_version':1,'marker':'TEST'}}
                if path=='diagnostics':return {'raw_csi':{'active':False}}
                if op=='patch':state['mode']=data['traffic_generator_mode'];return {}
                return {'traffic_generator_mode':state['mode']}
            def close(self):pass
        class Receiver:
            def __init__(self,*args,**kwargs):self.running=True
            def add_callback(self,callback):self.callback=callback
            def start_session(self):pass
            def bind_stream(self):pass
            def run(self, *, timeout, quiet):
                state['timeout'] = timeout
                for i in range(180):self.callback(owner.packet(i))
                ready.set()
                while self.running:time.sleep(.01)
            def stop(self):self.running=False
        class Generator:
            TRAFFIC_MARKER='TEST'
            def __init__(self,*args,**kwargs):pass
            def start(self):pass
            def stop(self):pass
        s=LiveSession(model=TEST_MODEL, clock=lambda: now[0]);original_start=s.start
        def simulated_start(target,repo,duration):original_start(target,repo,duration,(Client,Receiver,Generator))
        with patch.object(s,'start',simulated_start),patch.object(app,'LIVE',s):
            server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();url=f'http://127.0.0.1:{server.server_port}'
            def post(path,data):
                body=json.dumps(data).encode();req=Request(url+path,data=body,method='POST',headers={'Origin':url,'Content-Type':'application/json','X-WiFiPresence-Request':'1'})
                with urlopen(req,timeout=10) as response:return json.load(response)
            try:
                for options, timeout in [({'duration': 12}, 12), ({}, 0)]:
                    with self.subTest(options=options):
                        ready.clear()
                        post('/api/live/start',{'target':'192.168.1.50','room':'418',**options})
                        self.assertTrue(ready.wait(5))
                        self.assertEqual(state['timeout'], timeout)
                        with urlopen(url+'/api/live/status',timeout=5) as response:current=json.load(response)['live']
                        self.assertEqual((current['status'],current['presence'],current['packets']),('STREAMING','PRESENT',180))
                        if not options:
                            now[0] += 601
                            with urlopen(url+'/api/live/status',timeout=5) as response:current=json.load(response)['live']
                            self.assertEqual((current['status'], current['reason']), ('STREAMING', 'stale_stream'))
                        stopped=post('/api/live/stop',{})
                        self.assertEqual((stopped['status'],stopped['presence']),('STOPPED','UNKNOWN'))
                        self.assertTrue(stopped['session_closed']);self.assertTrue(stopped['restored'])
                        self.assertEqual(state['mode'],'internal')
            finally:s.stop();server.shutdown();server.server_close();thread.join(3)

    def test_local_api_rejects_cross_origin_and_invalid_input(self):
        server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start();url=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(url+'/api/live/status',timeout=5) as response:
                state=json.load(response);self.assertEqual(state['app'],'WiFiPresence-live-MVP');self.assertEqual(state['live']['presence'],'UNKNOWN')
            body=json.dumps({'target':'127.0.0.1','room':'418','duration':12}).encode()
            def send(origin):return urlopen(Request(url+'/api/live/start',data=body,method='POST',headers={'Origin':origin,'Content-Type':'application/json','X-WiFiPresence-Request':'1'}),timeout=5)
            with self.assertRaises(HTTPError) as denied:send('https://unrelated.example')
            self.assertEqual(denied.exception.code,403);denied.exception.close()
            with self.assertRaises(HTTPError) as invalid:send(url)
            self.assertEqual(invalid.exception.code,400);invalid.exception.close()
        finally:server.shutdown();server.server_close();t.join(3)

if __name__=='__main__':unittest.main()
