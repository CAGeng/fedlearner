import copy
import json
import logging
import os
import threading
import typing
from collections import defaultdict
from concurrent import futures

import grpc
from tensorflow.python.lib.io import file_io

import fedlearner.common.common_pb2 as common_pb
import fedlearner.common.private_set_union_pb2 as psu_pb
import fedlearner.common.private_set_union_pb2_grpc as psu_grpc
import fedlearner.common.transmitter_service_pb2 as tsmt_pb
from fedlearner.channel import Channel
from fedlearner.data_join.private_set_union import utils
from fedlearner.data_join.private_set_union.keys import get_keys

TRANS_FSM = {psu_pb.Encrypt: psu_pb.Sync,
             psu_pb.Sync: psu_pb.L_Diff,
             psu_pb.L_Diff: psu_pb.R_Diff,
             psu_pb.R_Diff: psu_pb.Reload,
             psu_pb.Reload: psu_pb.Reload}  # TODO(zhangzihui): Finish Phase?


class PSUTransmitterMasterService(psu_grpc.PSUTransmitterMasterServiceServicer):
    def __init__(self,
                 phase: str,
                 ready_event: threading.Event,
                 done_event: threading.Event,
                 peer_client: psu_grpc.PSUPhaseManagerServiceStub,
                 key_type,
                 file_paths: typing.List[str],
                 worker_num: int):
        self._peer = peer_client
        self._phase = getattr(psu_pb, phase.title())
        self._next_phase = self._phase
        self._transition_ready = ready_event
        self._transition_done = done_event

        self._key_info = get_keys(psu_pb.KeyInfo(type=key_type))
        self._file_paths = file_paths
        self._worker_num = worker_num
        self._meta_path = utils.Paths.encode_master_meta_path(phase)
        self._meta = self._get_meta()

        self._condition = threading.Condition()
        self._signal_buffer = defaultdict(set)
        self._finished_workers = set()
        super().__init__()

    def GetPhase(self, request, context):
        if not self._transition_done.is_set():
            # TODO(zhangzihui): more status
            return psu_pb.GetPhaseResponse(
                status=common_pb.Status(
                    code=common_pb.STATUS_WAIT_FOR_SYNCING_CHECKPOINT))
        return psu_pb.GetPhaseResponse(
            status=common_pb.Status(code=common_pb.STATUS_SUCCESS),
            phase=self._phase)

    def GetKeys(self, request, context):
        return psu_pb.GetKeysResponse(
            status=common_pb.Status(code=common_pb.STATUS_SUCCESS),
            key_info=self._key_info)

    def AllocateTask(self, request, context):
        if request.phase < self._phase:
            return psu_pb.PSUAllocateTaskResponse(
                status=common_pb.Status(code=common_pb.STATUS_NO_MORE_DATA))

        if self.data_finished:
            with self._condition:
                self._finished_workers.add(request.rank_id)
                self._condition.notify_all()
            return tsmt_pb.AllocateTaskResponse(
                status=common_pb.Status(code=common_pb.STATUS_NO_MORE_DATA))

        rid = request.rank_id
        alloc_files = []
        alloc_idx = []
        with self._condition:
            for i in range(rid, len(self._file_paths), self._worker_num):
                if i in self._meta['finished']:
                    continue
                alloc_files.append(self._file_paths[i])
                alloc_idx.append(i)

        if len(alloc_idx) == 0:
            with self._condition:
                self._finished_workers.add(request.rank_id)
                self._condition.notify_all()
            return tsmt_pb.AllocateTaskResponse(
                status=common_pb.Status(code=common_pb.STATUS_NO_MORE_DATA))

        return tsmt_pb.AllocateTaskResponse(
            status=common_pb.Status(code=common_pb.STATUS_SUCCESS),
            files=alloc_files,
            file_idx=alloc_idx)

    def FinishFiles(self, request, context):
        self._check_file_finished(request.phase, request.file_idx, 'send')
        return tsmt_pb.FinishFilesResponse(
            status=common_pb.Status(code=common_pb.STATUS_SUCCESS))

    def RecvFinishFiles(self, request, context):
        self._check_file_finished(request.phase, request.file_idx, 'recv')
        return tsmt_pb.FinishFilesResponse(
            status=common_pb.Status(code=common_pb.STATUS_SUCCESS))

    def _get_meta(self) -> dict:
        meta = self._read_meta()
        if meta:
            diff = set(self._file_paths) - set(meta['files'])
            if diff:
                for f in self._file_paths:
                    if f in diff:
                        meta['files'].append(f)
        else:
            meta = {
                'files': self._file_paths,
                'finished': set()
            }
        self._dump_meta(meta)
        return meta

    def _read_meta(self) -> [None, dict]:
        if file_io.file_exists(self._meta_path):
            meta_str = file_io.read_file_to_string(self._meta_path)
            meta = self.decode_meta(meta_str)
        else:
            meta = None
        return meta

    def _dump_meta(self, meta: dict) -> None:
        assert meta
        with self._condition:
            file_io.atomic_write_string_to_file(self._meta_path,
                                                self.encode_meta(meta))

    def _check_file_finished(self, phase, indices: typing.List[int], kind: str):
        assert kind in ('send', 'recv')
        opposite = 'send' if kind == 'recv' else 'recv'
        with self._condition:
            if phase != self._phase or not self._transition_done.is_set():
                return
            for idx in indices:
                if idx in self._meta['finished']:
                    continue
                if idx in self._signal_buffer[opposite]:
                    self._meta['finished'].add(idx)
                    self._signal_buffer[opposite].discard(idx)
                else:
                    self._signal_buffer[kind].add(idx)
            self._dump_meta(self._meta)
            self._condition.notify_all()

    @staticmethod
    def decode_meta(json_str: [str, bytes]) -> dict:
        meta = json.loads(json_str)
        meta['finished'] = set(meta['finish'])
        return meta

    @staticmethod
    def encode_meta(meta: dict) -> str:
        m = copy.deepcopy(meta)
        m['finished'] = list(m['finished'])
        return json.dumps(m)

    @property
    def data_finished(self):
        with self._condition:
            return len(self._meta['finished']) == len(self._file_paths)

    @property
    def finished(self):
        with self._condition:
            return self.data_finished \
                   and len(self._finished_workers) == self._worker_num

    def get_phases(self):
        with self._condition:
            return self._phase, self._next_phase

    def wait_to_enter_next_phase(self, file_paths: typing.List[str]):
        self.wait_for_finish()
        self._next_phase = TRANS_FSM[self._phase]
        # tell peer we are ready to enter next phase and wait if needed
        resp = self._peer.TransitionReady(psu_pb.TransitionRequest(
            curr_phase=self._phase,
            next_phase=self._next_phase
        ))
        if resp.curr_phase == self._phase \
                and resp.next_phase == self._next_phase:
            self._transition_ready.set()
            self._transition_done.clear()
        else:
            # peer not ready yet
            self._transition_ready.wait()

        with self._condition:
            # transitioning, lock
            self._phase = self._next_phase
            self._file_paths = file_paths
            self._meta_path = utils.Paths.encode_master_meta_path(
                psu_pb.Phase.keys()[self._phase])
            self._meta = self._get_meta()
            self._signal_buffer = defaultdict(set)
            self._finished_workers = set()

        # tell peer we've transitioned and wait for them if needed
        resp = self._peer.TransitionDone(
            psu_pb.TransitionRequest(curr_phase=self._phase,
                                     next_phase=self._next_phase)
        )
        if resp.curr_phase == resp.next_phase == self._phase:
            self._transition_done.set()
            self._transition_ready.clear()
        else:
            # peer not yet transitioned
            self._transition_done.wait()

    def wait_for_finish(self):
        with self._condition:
            while not self.finished:
                self._condition.wait()


class PSUPhaseManagerService(psu_grpc.PSUPhaseManagerServiceServicer):
    def __init__(self,
                 local_master: PSUTransmitterMasterService,
                 ready_event: threading.Event,
                 done_event: threading.Event):
        self._master = local_master
        self._transition_ready = ready_event
        self._transition_done = done_event

    def TransitionReady(self, request, context):
        peer_phase = request.curr_phase
        peer_next = request.next_phase
        curr_phase, next_phase = self._master.get_phases()
        resp = psu_pb.TransitionResponse(
            status=common_pb.Status(code=common_pb.STATUS_SUCCESS),
            curr_phase=curr_phase,
            next_phase=next_phase
        )

        # if phase transition condition not met
        if peer_phase == peer_next or curr_phase == next_phase:
            return resp

        # if current phase & next phase match
        if curr_phase == peer_phase and next_phase == peer_next:
            self._transition_ready.set()
            self._transition_done.clear()
            return resp

        # phases not match, not expected.
        return psu_pb.TransitionResponse(
            status=common_pb.Status(code=common_pb.STATUS_INVALID_REQUEST,
                                    error_message='Phases not comply.'),
            curr_phase=curr_phase,
            next_phase=next_phase
        )

    def TransitionDone(self, request, context):
        peer_phase = request.curr_phase
        peer_next = request.next_phase
        curr_phase, next_phase = self._master.get_phases()
        # if waiting for peer finish, set done and clear ready
        if peer_phase == peer_next == curr_phase == next_phase \
                and self._transition_ready.is_set():
            self._transition_done.set()
            self._transition_ready.clear()
        return psu_pb.TransitionResponse(
            status=common_pb.Status(code=common_pb.STATUS_SUCCESS),
            curr_phase=curr_phase,
            next_phase=next_phase)


class PSUTransmitterMaster:
    def __init__(self,
                 remote_listen_port: int,
                 remote_address: str,
                 local_listen_port: int,
                 phase: str,
                 key_type,
                 file_paths: typing.List[str],
                 worker_num: int):
        # channel and server
        self._listen_address = "[::]:{}".format(remote_listen_port)
        self._remote_address = remote_address
        self._token = 'PSUTransmitterMaster'
        self._channel = Channel(
            self._listen_address, self._remote_address, token=self._token)
        self._channel.subscribe(self._channel_callback)
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        self._server.add_insecure_port("[::]:{}".format(local_listen_port))

        ready_event = threading.Event()
        done_event = threading.Event()
        peer_client = psu_grpc.PSUPhaseManagerServiceStub(self._channel)
        self._local_servicer = PSUTransmitterMasterService(
            phase, ready_event, done_event, peer_client,
            key_type, file_paths, worker_num)
        self._peer_servicer = PSUPhaseManagerService(
            self._local_servicer, ready_event, done_event)

        psu_grpc.add_PSUTransmitterMasterServiceServicer_to_server(
            self._local_servicer, self._server)
        psu_grpc.add_PSUPhaseManagerServiceServicer_to_server(
            self._peer_servicer, self._channel)

        self._started = False
        self._condition = threading.Condition()

    def _channel_callback(self, channel, event):
        if event == Channel.Event.PEER_CLOSED:
            with self._condition:
                self._peer_terminated = True
                self._condition.notify_all()
        if event == Channel.Event.ERROR:
            err = channel.error()
            logging.fatal("[Bridge] suicide due to channel exception: %s, "
                          "may be caused by peer restart", repr(err))
            os._exit(138)  # Tell Scheduler to restart myself

    def run(self):
        if not self._started:
            self._server.start()
            self._channel.connect()
            self._started = True

    def wait_for_finish(self):
        self._local_servicer.wait_for_finish()

    def stop(self):
        if self._started:
            self._server.stop()
            self._channel.close()
            self._started = False
