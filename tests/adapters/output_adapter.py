"""Real byte-output capability for disposable local integration adapters."""
from __future__ import print_function

import binascii
import hashlib
import os
import posixpath
import sys

from aap_semantics import MemoryOutputSink, OutputResult


class StdoutOutputSink(MemoryOutputSink):
    """Write ordinary print request bytes to this CLI process's stdout.

    The binary stream is looked up for each event so inherited pipes and file
    redirections remain the actual destination.  Events remain observable.
    """
    def __init__(self, stream_provider=None):
        super(StdoutOutputSink, self).__init__()
        self.stream_provider = stream_provider or self._binary_stdout

    def _binary_stdout(self):
        stream = sys.stdout
        binary = getattr(stream, 'buffer', None)
        if binary is None:
            raise IOError('CLI stdout has no binary stream')
        return binary

    def emit(self, request):
        self.events.append(request)
        if type(request.data) is not bytes:
            raise TypeError('ordinary print output must be encoded bytes')
        stream = self.stream_provider()
        offset = 0
        while offset < len(request.data):
            written = stream.write(request.data[offset:])
            if not isinstance(written, int) or written <= 0:
                raise IOError('short CLI stdout write')
            offset += written
        stream.flush()
        return OutputResult()


class LocalByteSession(object):
    """Opaque session retaining ordered binary writes and explicit close."""
    def __init__(self, stream, path, mode, record):
        self._stream = stream
        self._path = path
        self._mode = mode
        self._record = record
        self._closed = False
        self._operation = 0

    def write(self, data):
        if self._closed:
            raise ValueError('byte output session is closed')
        if type(data) is not bytes:
            raise TypeError('byte output session accepts bytes only')
        self._operation += 1
        try:
            written = self._stream.write(data)
            if written != len(data):
                raise IOError('short byte output write')
        except Exception as error:
            self._record('byte_output_write', path=self._path, mode=self._mode,
                         operation=self._operation, status='FAILED',
                         bytes=len(data), detail=str(error))
            raise
        self._record('byte_output_write', path=self._path, mode=self._mode,
                     operation=self._operation, status='COMPLETED',
                     bytes=len(data),
                     data_hex=binascii.hexlify(data).decode('ascii'))

    def close(self):
        if self._closed:
            raise ValueError('byte output session is already closed')
        self._operation += 1
        try:
            self._stream.close()
        except Exception as error:
            self._record('byte_output_close', path=self._path, mode=self._mode,
                         operation=self._operation, status='FAILED',
                         detail=str(error))
            raise
        self._closed = True
        self._record('byte_output_close', path=self._path, mode=self._mode,
                     operation=self._operation, status='COMPLETED',
                     after=LocalByteWriter._snapshot(self._path))


class LocalByteWriter(object):
    """Adapt a resolved shell-helper request to a local file byte session.

    This lives in the integration adapter. Semantic/runtime code only sees the
    request and the opaque write/close session returned here.
    """
    def __init__(self, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)

    def _path(self, request):
        path = request.path
        if not posixpath.isabs(path):
            if type(request.cwd) is not str or not posixpath.isabs(request.cwd):
                raise ValueError('byte output requires an absolute logical cwd')
            path = posixpath.join(request.cwd, path)
        return path

    @staticmethod
    def _mode(request):
        """Bridge the established shell-helper and :cat request vocabularies."""
        mode = getattr(request, 'mode', None)
        if mode in ('w', 'a'):
            return mode
        destination = getattr(request, 'destination', None)
        if destination == 'overwrite':
            return 'w'
        if destination == 'append':
            return 'a'
        raise ValueError('unsupported byte output mode: ' + str(mode or destination))

    @staticmethod
    def _snapshot(path):
        """Best-effort evidence only; a tracing failure must not block output."""
        if not os.path.lexists(path):
            return {'kind': 'missing'}
        if not os.path.isfile(path):
            return {'kind': 'non-regular'}
        try:
            with open(path, 'rb') as stream:
                data = stream.read()
            return {'kind': 'regular', 'bytes': len(data),
                    'sha256': hashlib.sha256(data).hexdigest(),
                    'data_hex': binascii.hexlify(data).decode('ascii')}
        except Exception as error:
            return {'kind': 'regular', 'observation_error': str(error)}

    def open_bytes(self, request):
        path = self._path(request)
        mode = self._mode(request)
        modes = {'w': 'wb', 'a': 'ab'}
        self._record('byte_output_open', path=path, requested_path=request.path,
                     cwd=request.cwd, mode=mode, status='STARTED',
                     before=self._snapshot(path))
        try:
            stream = open(path, modes[mode])
        except Exception as error:
            self._record('byte_output_open', path=path,
                         requested_path=request.path, cwd=request.cwd,
                         mode=mode, status='FAILED', detail=str(error))
            raise
        self._record('byte_output_open', path=path, requested_path=request.path,
                     cwd=request.cwd, mode=mode, status='COMPLETED')
        return LocalByteSession(stream, path, mode, self._record)
