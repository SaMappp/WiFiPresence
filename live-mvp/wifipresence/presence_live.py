"""Explicit, bounded live raw-CSI adapter; optional upstream dependency only."""
from __future__ import annotations

from collections import Counter, deque
from contextlib import nullcontext
import importlib.util
import ipaddress
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np

from .core import Capture, Config, DataError
from .empty_reference import (EmptyReference, MIN_REFERENCE_WINDOWS, MIN_REFERENCE_WINDOWS_PER_HALF,
                              REFERENCE_CHECK_SECONDS, REFERENCE_SECONDS, confirm_sustained_presence)
from .presence_model import feature_windows, infer

PINNED_COMMIT = '12fce9354d3506290e70a853a4f22866f0e987ef'
DEVICE_STREAM_ENDED = 'RuntimeError: Direct raw HTTP session ended on the device'


def load_upstream(repo):
    repo = Path(repo).resolve()
    revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True, timeout=5).strip()
    if revision != PINNED_COMMIT:
        raise DataError('Live adapter requires the pinned ESPectre 3.0.0 source checkout.')
    if str(repo) not in sys.path: sys.path.insert(0, str(repo))
    from tools.lib.csi_io import DirectRawCSIReceiver
    from src.python.espectre_cli.device_transport import DirectClient
    from tools.ha_traffic_generator_addon.espectre_traffic_generator import ExternalTrafficGenerator
    return DirectClient, DirectRawCSIReceiver, ExternalTrafficGenerator


class LiveSession:
    def __init__(self, *, model=None, room='418', clock=time.monotonic, calibrated_live=False, reference_store=None):
        self.model, self.room, self.clock = model, room, clock
        self.calibrated_live = calibrated_live
        self.reference_store = reference_store
        self.lock = threading.RLock()
        self.buffer = deque(maxlen=1000)
        self.thread = None
        self.receiver = None
        self.stop_event = threading.Event()
        self.generation = 0
        self.reset()

    def reset(self):
        with self.lock:
            self.generation += 1
            self.buffer.clear()
            self.status = 'STOPPED'
            self.error = None
            self.total = 0
            self.invalid_packets = 0
            self.last_arrival = None
            self.metadata = {}
            self.restored = None
            self.session_closed = None
            self.original_mode = None
            self.transport_problem = False
            # Minimal local diagnostics: recorded for traceability only; they
            # never change the presence/UNKNOWN decision or the quality gates.
            self.error_stage = None
            self.observed_pps = None
            self.last_window = {}
            self.reason_counts = Counter()
            self.last_reason = None
            self.last_reason_at = None
            self.diag_polls = 0
            self.transport_diag = {'drops': None, 'backpressure': None, 'first_seen_packet': None}
            self.last_transport_failure = None
            self.reconnect_attempts = 0
            self.session_started = None
            self.empty_reference = None
            self.reference_samples = []
            self.reference_profile = None
            self.reference_elapsed_s = 0.0
            self.reference_state = 'unconfirmed' if self.calibrated_live else 'disabled'
            self.reference_mode = 'fresh'
            self.reference_required_s = REFERENCE_SECONDS
            self.reference_key = None
            self.pending_reference = None
            self.reference_saved = None
            self.reference_quality = None
            self.stream_deadline = None

    def accept_packet(self, packet):
        raw = np.asarray(packet.iq_raw).reshape(-1)
        if (raw.dtype != np.int8 or raw.shape != (128,) or packet.device_ticks_us is None
                or (packet.phy_mode, packet.ltf_type, str(packet.channel_width)) != ('ht', 'ht-ltf', '20')):
            with self.lock: self.invalid_packets += 1
            return
        amplitude = np.hypot(raw[0::2].astype(float), raw[1::2].astype(float))
        if not np.any(raw):
            with self.lock: self.invalid_packets += 1
            return
        tick = int(packet.device_ticks_us)
        with self.lock:
            if self.buffer and tick <= self.buffer[-1][0]: self.buffer.clear()
            self.buffer.append((tick, amplitude))
            while self.buffer and tick - self.buffer[0][0] > 2_200_000: self.buffer.popleft()
            self.total += 1
            self.last_arrival = self.clock()
            self.metadata = {'device_id': str(packet.device_id), 'firmware_version': str(packet.firmware_version),
                             'requested_pps': 100.0}
            if self.calibrated_live:
                self._collect_reference(tick, amplitude)
            drops = int(getattr(packet, 'raw_drop_total', 0) or 0)
            backpressure = int(getattr(packet, 'raw_send_backpressure_total', 0) or 0)
            if drops or backpressure:
                if not self.transport_problem:
                    self.transport_diag['first_seen_packet'] = self.total
                self.transport_problem = True
                if self.reference_state in ('collecting', 'buffered', 'ready'):
                    self.reference_state = 'invalidated'
                    self.empty_reference = None
                    self.reference_samples.clear()
            self.transport_diag['drops'] = drops
            self.transport_diag['backpressure'] = backpressure

    def _collect_reference(self, tick, amplitude):
        if self.reference_state not in ('collecting', 'buffered', 'ready'):
            return
        profile = tuple(self.metadata.items())
        if self.reference_profile is None:
            self.reference_profile = profile
            self.reference_last_tick = tick - 1
        if profile != self.reference_profile or tick <= self.reference_last_tick:
            self.reference_state = 'invalidated'
            self.empty_reference = None
            self.reference_samples.clear()
            return
        self.reference_last_tick = tick
        if self.reference_state in ('collecting', 'buffered'):
            if self.reference_samples:
                self.reference_elapsed_s = (tick - self.reference_samples[0][0]) / 1_000_000
            if self.reference_elapsed_s < REFERENCE_SECONDS and len(self.reference_samples) < 4000:
                self.reference_samples.append((tick, amplitude))
            elif self.reference_elapsed_s < REFERENCE_SECONDS:
                self.reference_state = 'invalidated'
                self.reference_samples.clear()
                return
            if self.reference_elapsed_s >= self.reference_required_s:
                self.reference_state = 'buffered'

    def start(self, target, repo, duration=60, factories=None, *, empty_confirmed=False, reuse_reference=False):
        address = ipaddress.ip_address(target)
        if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast or str(address).endswith('.255'):
            raise DataError('Enter the single ESP32 private-LAN IPv4 address, not a broadcast or URL.')
        if duration is not None and (type(duration) is not int or not 3 <= duration <= 600):
            raise DataError('Set an integer duration of 3-600 seconds, or omit it for continuous detection.')
        if type(empty_confirmed) is not bool or type(reuse_reference) is not bool:
            raise DataError('Empty reference choices must be boolean.')
        if reuse_reference and not empty_confirmed:
            raise DataError('Reusing an empty reference requires a confirmed empty room.')
        if duration is not None and self.calibrated_live and empty_confirmed and duration < REFERENCE_SECONDS + 10:
            raise DataError('Allow at least 34 seconds for the empty reference and measurement.')
        with self.lock:
            if self.thread and self.thread.is_alive(): raise DataError('A live session is already active.')
            self.reset(); self.stop_event.clear(); self.receiver = None
            if self.calibrated_live and empty_confirmed:
                self.reference_state = 'collecting'
                if self.model and self.reference_store is not None:
                    self.reference_key = {'target': str(address), 'profile': self.model['profile'],
                                          'config': self.model['config']}
                    if reuse_reference:
                        self.pending_reference = self.reference_store.load(self.reference_key)
                        if self.pending_reference is not None:
                            self.reference_required_s = REFERENCE_CHECK_SECONDS
                            self.reference_mode = 'checking_saved'
            self.session_started = self.clock()
            self.status = 'CONNECTING'
            self.thread = threading.Thread(target=self._run, args=(str(address), repo, duration, factories), daemon=True)
            self.thread.start()

    def _run(self, target, repo, duration, factories):
        deadline = self._run_once(target, repo, factories, duration=duration)
        with self.lock:
            retry = (self.error_stage == 'stream' and self.last_transport_failure is not None
                     and self.session_closed is True and self.restored is True
                     and self.empty_reference is not None and self.reference_state == 'ready'
                     and not self.transport_problem and not self.stop_event.is_set()
                     and (duration is None or (deadline is not None and deadline - self.clock() >= 8)))
            if retry:
                original_mode = self.original_mode
                self.generation += 1
                self.buffer.clear()
                self.last_arrival = None
                self.last_window = {}
                self.metadata = {}
                self.status = 'RECONNECTING'
        if retry and not self.stop_event.wait(.5):
            with self.lock:
                self.error = self.error_stage = None
                self.reconnect_attempts = 1
                self.transport_diag.update(drops=None, backpressure=None, first_seen_packet=None)
            self._run_once(target, repo, factories, deadline=deadline, expected_mode=original_mode)
        elif retry:
            with self.lock:
                self.error = self.error_stage = None
        with self.lock:
            self.status = 'ERROR' if self.error else 'STOPPED'

    def _run_once(self, target, repo, factories, *, duration=None, deadline=None, expected_mode=None):
        control, receiver, generator, timer = None, None, None, nullcontext()
        mode_attempted = False
        session_attempted = False
        stage = 'connect'
        raw = {}
        with self.lock:
            if self.stop_event.is_set(): return deadline
            self.restored = self.session_closed = None
        try:
            client_cls, receiver_cls, generator_cls = factories or load_upstream(repo)
            timer_path = Path(__file__).resolve().parents[1] / 'capture' / 'WiFiPresence-Collector-Runner.py'
            spec = importlib.util.spec_from_file_location('presence_timer_runner', timer_path)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            timer = module.collection_timer()
            control = client_cls(f'http://{target}:62587/espectre/v1', timeout=3.0)
            stage = 'control_request'
            device = control.request('get', 'device')
            if device.get('chip') != 'esp32c6' or device.get('firmware') != '3.0.0':
                raise DataError('Device/profile is not the tested C6 Native 3.0.0 target.')
            sensing = control.request('get', 'sensing')
            original = sensing.get('traffic_generator_mode')
            if not isinstance(original, str) or not original: raise DataError('Cannot preserve original traffic mode.')
            if expected_mode is not None and original != expected_mode:
                raise DataError('Traffic mode changed before reconnect; automatic reconnect cancelled.')
            self.original_mode = original
            raw_before = control.request('get', 'diagnostics', {'fields': ['raw_csi']}).get('raw_csi', {})
            if raw_before.get('active') is not False:
                self.session_closed = False if raw_before.get('active') is True else None
                raise DataError('Another raw CSI session is active, or its status cannot be confirmed. Stop the other session first.')
            self.session_closed = True
            caps = control.request('get', 'capabilities').get('csi', {})
            if caps.get('protocol_version') != 1 or caps.get('marker') != generator_cls.TRAFFIC_MARKER:
                raise DataError('Raw capability/traffic marker mismatch.')
            if self.stop_event.is_set() or (deadline is not None and self.clock() >= deadline): return deadline
            if original != 'external':
                # A failed network request may still have applied the change.
                mode_attempted = True
                control.request('patch', 'sensing', {'traffic_generator_mode': 'external'})
            stage = 'stream_setup'
            receiver = receiver_cls(f'http://{target}:62587', buffer_size=500, derive_complex=False, timeout=3.0)
            self.receiver = receiver
            receiver.requested_pps = 100.0
            receiver.add_callback(self.accept_packet)
            generator = generator_cls([target], port=int(caps.get('traffic_udp_port', 5555)), rate_pps=100)
            with timer:
                session_attempted = True
                self.session_closed = None
                receiver.start_session(); generator.start(); receiver.bind_stream()
                with self.lock:
                    if self.stop_event.is_set(): return deadline
                    now = self.clock()
                    if deadline is None and duration is not None:
                        deadline = now + duration
                    if deadline is not None and now >= deadline: return deadline
                    self.stream_deadline = deadline
                    self.status = 'STREAMING'
                stage = 'stream'
                # The pinned receiver uses a non-positive timeout for an open-ended stream.
                receiver.run(timeout=0 if deadline is None else deadline - now, quiet=True)
        except Exception as exc:
            with self.lock:
                self.error = f'{type(exc).__name__}: {exc}'
                self.error_stage = stage
                self.status = 'STOPPING'
        finally:
            with self.lock:
                self.status = 'STOPPING'
            stage = 'stop'
            for resource in (generator, receiver):
                if resource:
                    try: resource.stop()
                    except Exception as exc: self.error = self.error or str(exc)
            if control:
                stage = 'cleanup'
                try:
                    if self.original_mode is not None and (mode_attempted or session_attempted):
                        # Closing TCP and firmware releasing CSI are asynchronous.
                        cleanup_deadline = time.monotonic() + 3.0
                        self.session_closed = False
                        while time.monotonic() < cleanup_deadline:
                            raw = control.request('get', 'diagnostics', {'fields': ['raw_csi']}).get('raw_csi', {})
                            if raw.get('active') is False:
                                self.session_closed = True
                                break
                            time.sleep(.1)
                        current = control.request('get', 'sensing').get('traffic_generator_mode')
                        if current != self.original_mode:
                            if not self.session_closed:
                                raise RuntimeError('Raw session did not close; original traffic source restoration deferred.')
                            control.request('patch', 'sensing', {'traffic_generator_mode': self.original_mode})
                        restored = control.request('get', 'sensing').get('traffic_generator_mode')
                        self.restored = restored == self.original_mode
                        if not self.restored: self.error = self.error or 'Original traffic mode was not restored.'
                        if not self.session_closed: self.error = self.error or 'Raw session closure not confirmed.'
                except Exception as exc:
                    self.restored = False; self.error = self.error or str(exc)
                    self.error_stage = self.error_stage or 'cleanup'
                finally: control.close()
            with self.lock:
                if (self.error_stage == 'stream' and (self.error or '').startswith(DEVICE_STREAM_ENDED)
                        and type(raw.get('send_backpressure_total')) is int and raw['send_backpressure_total'] > 0):
                    self.transport_diag['drops'] = raw.get('raw_drop_total', self.transport_diag['drops'])
                    self.transport_diag['backpressure'] = raw['send_backpressure_total']
                    self.last_transport_failure = dict(self.transport_diag, received_packets=self.total)
                self.receiver = None
                self.reference_samples.clear()
        return deadline

    def stop(self):
        with self.lock:
            self.stop_event.set()
            if self.thread and self.thread.is_alive(): self.status = 'STOPPING'
            if self.receiver is not None: self.receiver.running = False
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(timeout=8)
        if self.thread and self.thread.is_alive():
            with self.lock:
                self.error = 'Live worker has not stopped; data output is UNKNOWN.'
                self.status = 'ERROR'

    def _record(self, result):
        """Attach session diagnostics after the decision is made; never alters it."""
        with self.lock:
            reason = result.get('reason') or 'unknown'
            self.reason_counts[reason] += 1
            self.diag_polls += 1
            if reason != self.last_reason:
                self.last_reason = reason
                self.last_reason_at = self.clock()
            result['diagnostics'] = {
                'scope': 'status poll samples; not independent windows',
                'polls': self.diag_polls,
                'reason_counts': dict(self.reason_counts),
                'last_reason': self.last_reason,
                'last_reason_age_s': None if self.last_reason_at is None else round(self.clock() - self.last_reason_at, 3),
                'observed_pps': self.observed_pps,
                'received_packets': self.total,
                'invalid_packets': self.invalid_packets,
                'transport': dict(self.transport_diag),
                'last_transport_failure': dict(self.last_transport_failure) if self.last_transport_failure else None,
                'reconnect_attempts': self.reconnect_attempts,
                'last_window': dict(self.last_window),
                'error_stage': self.error_stage,
                'cleanup': {'session_closed': self.session_closed, 'restored': self.restored},
                'empty_reference': {'state': self.reference_state,
                                    'required_s': self.reference_required_s,
                                    'elapsed_s': round(min(self.reference_elapsed_s, self.reference_required_s), 1),
                                    'mode': self.reference_mode, 'saved': self.reference_saved,
                                    'quality': self.reference_quality,
                                    'windows': self.empty_reference.window_count if self.empty_reference else 0},
            }
        return result

    def _prepare_reference(self):
        # Use the same time window in the fast check and its offline regression.
        span = self.reference_required_s
        first_tick = self.reference_samples[0][0]
        samples = [(tick, a) for tick, a in self.reference_samples if tick - first_tick < span * 1_000_000]
        ticks = np.array([tick for tick, _ in samples], dtype=np.float64)
        capture = Capture(Path('LIVE_EMPTY_REFERENCE'), (ticks - ticks[0]) / 1e6,
                          np.array([a for _, a in samples]), 100.0, span, False, '', dict(self.metadata))
        config = Config(**self.model['config']) if self.model else Config()
        expected = self.model['profile'] if self.model else {}
        if self.reference_key and (self.room != expected.get('room') or any(
                str(expected.get(k)) != str(self.metadata.get(k)) for k in ('device_id', 'firmware_version', 'requested_pps'))):
            self.reference_state = 'invalidated'
            self.reference_samples.clear()
            return
        rows = feature_windows(capture, config)
        if self.reference_mode == 'checking_saved':
            saved = self.pending_reference
            self.pending_reference = None
            if saved.matches_empty_check(rows):
                self.empty_reference = saved
                self.reference_mode = 'reused'
                self.reference_state = 'ready'
                self.reference_saved = True
                self.reference_samples.clear()
            else:
                # Preserve the uninterrupted confirmed-empty prefix for the full fit.
                self.reference_required_s = REFERENCE_SECONDS
                self.reference_mode = 'fresh_after_mismatch'
                self.reference_state = 'buffered' if self.reference_elapsed_s >= REFERENCE_SECONDS else 'collecting'
            return
        self.reference_samples.clear()
        eligible = [row for row in rows if row['features'] is not None]
        early = sum(row['start_s'] < span / 2 for row in eligible)
        self.reference_quality = {
            'valid_windows': len(eligible), 'total_windows': len(rows),
            'valid_per_half': [early, len(eligible) - early],
            'required_windows': MIN_REFERENCE_WINDOWS, 'required_per_half': MIN_REFERENCE_WINDOWS_PER_HALF,
            'median_slot_coverage': float(np.median([row['occupancy'] for row in rows])),
            'minimum_slot_coverage': config.min_occupancy,
        }
        self.empty_reference = EmptyReference.fit(rows)
        if self.empty_reference is None:
            if (self.reference_mode != 'fresh_after_quality_retry'
                    and (self.stream_deadline is None or self.stream_deadline - self.clock() >= REFERENCE_SECONDS + 10)):
                self.reference_mode = 'fresh_after_quality_retry'
                self.reference_elapsed_s = 0.0
                self.reference_state = 'collecting'
            else:
                self.reference_state = 'insufficient'
            return
        self.reference_state = 'ready'
        if self.reference_key is not None:
            try:
                self.reference_store.save(self.reference_key, self.empty_reference)
                self.reference_saved = True
            except OSError:
                self.reference_saved = False

    def snapshot(self):
        with self.lock:
            samples = list(self.buffer)
            if len(samples) >= 2:
                span_s = (samples[-1][0] - samples[0][0]) / 1e6
                recent = sum(1 for tick, _ in samples if samples[-1][0] - tick <= 1_000_000)
                self.observed_pps = round(recent / min(1.0, span_s), 2) if span_s > 0 else None
            else:
                self.observed_pps = None
            result = {'source': 'LIVE', 'status': self.status, 'presence': 'UNKNOWN', 'reason': 'live_not_streaming',
                      'packets': self.total, 'invalid_packets': self.invalid_packets, 'error': self.error,
                      'original_mode': self.original_mode, 'restored': self.restored,
                      'session_closed': self.session_closed, 'windows': []}
            if self.status == 'RECONNECTING':
                result['reason'] = 'raw_transport_reconnecting'
            if self.status != 'STREAMING': return self._record(result)
            if self.last_arrival is None or self.clock() - self.last_arrival > 3:
                result['reason'] = 'waiting_for_packets' if self.last_arrival is None else 'stale_stream'
                return self._record(result)
            if self.transport_problem:
                result['reason'] = 'raw_transport_drops_or_backpressure'; return self._record(result)
            meta = dict(self.metadata)
            generation = self.generation
            if self.reference_state == 'buffered':
                self._prepare_reference()
            reference = self.empty_reference
            reference_reason = {'unconfirmed': 'empty_reference_required', 'collecting': 'empty_reference_collecting',
                                'insufficient': 'empty_reference_insufficient', 'invalidated': 'empty_reference_invalidated'}.get(self.reference_state)
        if len(samples) < 2 or samples[-1][0] - samples[0][0] < 1_000_000:
            result['reason'] = 'window_warmup'; return self._record(result)
        ticks = np.array([v[0] for v in samples], float)
        times = (ticks - ticks[0]) / 1e6
        c = Capture(Path('LIVE'), times, np.array([v[1] for v in samples]), 100.0,
                    float(times[-1] + .01), False, '', meta)
        if self.calibrated_live:
            rows = infer(c, self.model, admitted=True, room=self.room, reference=reference,
                         reference_reason=reference_reason or 'empty_reference_required')
            if reference is not None:
                confirm_sustained_presence(rows)
        else:
            rows = infer(c, self.model, admitted=True, room=self.room)
        with self.lock:
            if (self.generation != generation or self.status != 'STREAMING' or self.stop_event.is_set()
                    or self.transport_problem or self.last_arrival is None or self.clock() - self.last_arrival > 3
                    or (reference is not None and reference is not self.empty_reference)):
                result.update({'status': self.status, 'presence': 'UNKNOWN', 'reason': 'stream_changed_during_processing',
                               'error': self.error, 'restored': self.restored, 'session_closed': self.session_closed})
                return self._record(result)
            last = rows[-1]
            self.last_window = {k: last.get(k) for k in
                                ('start_s', 'end_s', 'occupancy', 'max_gap_s', 'quality',
                                 'presence', 'model_score', 'reference_distance', 'reason')}
            result.update({'presence': rows[-1]['presence'], 'reason': rows[-1]['reason'], 'windows': rows,
                           'quality_scope': 'online project window checks; not official whole-file admission',
                           'model_loaded': self.model is not None})
        return self._record(result)
