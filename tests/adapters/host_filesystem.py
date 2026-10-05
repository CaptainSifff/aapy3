"""Real host filesystem capabilities for the integration CLI."""
from __future__ import print_function

import hashlib
import os
import shutil

from aap_semantics import (ActionWorkspace, ArtifactBackend, CopyBackend, CopyObservation, CopyResult, DeleteBackend, DeleteResult, FileState, MarkerBackend, MoveBackend, MoveResult, OutputResult, PathObservation, PathObserver, PortDirectories, TargetStateBackend, TreeFilesystem, TreeObservation)
from output_adapter import LocalByteWriter


class Files(ArtifactBackend):
    def __init__(self, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)

    def exists(self, path):
        return os.path.isfile(path)

    def chunks(self, path):
        self._record('artifact_read', path=path, status='STARTED')
        if not os.path.exists(path):
            error = OSError('artifact is missing: ' + path)
            self._record('artifact_read', path=path, path_kind='missing',
                         status='FAILED', detail=str(error))
            raise error
        if not os.path.isfile(path):
            error = OSError('artifact is not a regular file: ' + path)
            self._record('artifact_read', path=path, path_kind='non-regular',
                         status='FAILED', detail=str(error))
            raise error
        digest, count = hashlib.sha256(), 0
        try:
            with open(path, 'rb') as stream:
                while True:
                    data = stream.read(32768)
                    if not data:
                        break
                    digest.update(data)
                    count += len(data)
                    yield data
        except Exception as error:
            self._record('artifact_read', path=path, path_kind='regular',
                         status='FAILED', bytes=count, detail=str(error))
            raise
        self._record('artifact_read', path=path, path_kind='regular',
                     status='COMPLETED', bytes=count,
                     sha256=digest.hexdigest())


class Directories(PortDirectories):
    def enter(self, path):
        if not os.path.isdir(path):
            raise OSError('directory missing or inaccessible: ' + path)
        return os.path.realpath(path)


class Workspace(ActionWorkspace):
    def prepare_directory(self, path):
        if not os.path.isdir(path):
            os.makedirs(path)
        return os.path.realpath(path)

    def file_kind(self, path):
        if os.path.isdir(path):
            return 'directory'
        return 'file' if os.path.isfile(path) else 'missing'

    def filetype(self, path):
        return None


class Markers(MarkerBackend):
    def __init__(self, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)

    def marker_exists(self, path):
        exists = os.path.exists(path)
        self._record('marker_lookup', path=path, exists=exists)
        return exists

    def path_kind(self, path):
        if not os.path.exists(path):
            return 'missing'
        return 'directory' if os.path.isdir(path) else 'other'

    def mkdir(self, path, mode=None, require_parent=False):
        if require_parent:
            if mode is None:
                os.mkdir(path)
            else:
                os.mkdir(path, mode)
            return
        if mode is not None:
            os.mkdir(path, mode)
            return
        if not os.path.isdir(path):
            os.makedirs(path)

    def touch(self, path):
        with open(path, 'ab'):
            pass
        os.utime(path, None)

    def create_exclusive(self, path):
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        os.close(descriptor)


class Paths(PathObserver):
    def observe(self, request):
        return PathObservation('EXISTS' if os.path.exists(request.path) else 'MISSING')


class Writer(LocalByteWriter):
    def __init__(self, record=None):
        super(Writer, self).__init__(record if record is not None else
                                      (lambda kind, **fields: None))

    def write(self, request):
        parent = os.path.dirname(request.path)
        if not os.path.isdir(parent):
            raise OSError('output parent unavailable: ' + parent)
        mode = 'wb' if request.destination == 'overwrite' else 'ab'
        with open(request.path, mode) as stream:
            stream.write(request.data)
        return OutputResult()


class Trees(TreeFilesystem):
    def list_directory(self, request):
        try:
            return TreeObservation('ENTRIES', os.listdir(request.path))
        except FileNotFoundError:
            return TreeObservation('MISSING')
        except OSError as error:
            return TreeObservation('UNREADABLE', detail=str(error))

    def classify(self, request):
        try:
            if os.path.islink(request.path):
                if os.path.isdir(request.path):
                    target = 'DIRECTORY'
                elif os.path.isfile(request.path):
                    target = 'FILE'
                else:
                    target = 'MISSING'
                return TreeObservation('SYMLINK', target_kind=target)
            if os.path.isdir(request.path):
                return TreeObservation('DIRECTORY')
            if os.path.isfile(request.path):
                return TreeObservation('FILE')
            return TreeObservation('MISSING')
        except OSError as error:
            return TreeObservation('ERROR', detail=str(error))


class Delete(DeleteBackend):
    def delete_tree(self, request):
        try:
            if os.path.islink(request.path) or os.path.isfile(request.path):
                os.unlink(request.path)
            elif os.path.isdir(request.path):
                shutil.rmtree(request.path)
            else:
                return DeleteResult('FAILED', 'path disappeared before deletion')
            return DeleteResult('COMPLETED')
        except OSError as error:
            return DeleteResult('FAILED', str(error))


class Move(MoveBackend):
    def move(self, request):
        try:
            os.rename(request.source, request.destination)
            return MoveResult()
        except OSError as error:
            return MoveResult('FAILED', str(error))


class Copy(CopyBackend):
    """Real local regular-file adapter for the bounded :copy runtime."""
    def __init__(self, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)

    def path_kind(self, path):
        if os.path.isfile(path):
            outcome = CopyObservation(kind='regular')
        elif os.path.isdir(path):
            outcome = CopyObservation(kind='directory')
        elif os.path.lexists(path):
            outcome = CopyObservation(kind='other')
        else:
            outcome = CopyObservation(kind='missing')
        self._record('copy_observation', path=path, path_kind=outcome.kind)
        return outcome

    def _fingerprint(self, path):
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            while True:
                data = stream.read(32768)
                if not data:
                    break
                digest.update(data)
        return {'bytes': os.path.getsize(path), 'sha256': digest.hexdigest()}

    def copy(self, request):
        self._record('copy_request', source=request.source,
                     destination_argument=request.destination_argument,
                     effective_destination=request.effective_destination,
                     cwd=request.cwd, source_kind=request.source_kind,
                     destination_kind=request.destination_kind,
                     overwrite=request.overwrite)
        try:
            shutil.copy(request.source, request.effective_destination)
            self._record('copy_complete', source=request.source,
                         effective_destination=request.effective_destination,
                         source_present=os.path.isfile(request.source),
                         destination_present=os.path.isfile(request.effective_destination),
                         source_fingerprint=self._fingerprint(request.source),
                         destination_fingerprint=self._fingerprint(request.effective_destination))
            return CopyResult()
        except (IOError, OSError) as error:
            self._record('copy_failed', source=request.source,
                         effective_destination=request.effective_destination,
                         detail=str(error))
            return CopyResult('FAILED', str(error))


class HostState(TargetStateBackend):
    def file_state(self, node):
        path = node.path
        try:
            value = os.stat(path)
            return FileState(True, value.st_mtime, os.path.isdir(path))
        except OSError:
            return FileState(False, 0, False)

    def current_signature(self, node, check):
        state = self.file_state(node)
        if check == 'time':
            return str(state.mtime if state.exists else 0)
        if check == 'none':
            return 'unknown'
        if check in ('md5', 'c_md5'):
            if not state.exists or state.directory:
                return 'unknown'
            digest = hashlib.md5()
            with open(node.path, 'rb') as stream:
                while True:
                    data = stream.read(32768)
                    if not data:
                        break
                    digest.update(data)
            return digest.hexdigest()
        return None

    def stored_signature(self, target, source, check):
        return ''

    def build_signature(self, definition, target):
        return None

    def implicit_dependencies(self, node):
        return False

    def matching_rule(self, node):
        return False

