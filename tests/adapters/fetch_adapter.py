"""Real byte acquisition adapter for the bounded port fetch capability."""
from __future__ import print_function

import hashlib
import http.client
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request

from aap_semantics.fetch import FetchBackend, FetchAttempt, FetchResult


class LocalFetchBackend(FetchBackend):
    def __init__(self, record=None, timeout=20):
        self.record = record if record is not None else (lambda kind, **fields: None)
        self.timeout = timeout

    def _fingerprint(self, path):
        digest = hashlib.sha256()
        count = 0
        with open(path, 'rb') as stream:
            while True:
                chunk = stream.read(32768)
                if not chunk:
                    break
                digest.update(chunk)
                count += len(chunk)
        return count, digest.hexdigest()

    def _file(self, request, candidate):
        # VersCont.separate_scheme removes file:// and Cache.local_name then
        # resolves the remainder from the process cwd without percent decoding.
        name = candidate[len('file://'):]
        source = os.path.expanduser(name)
        if not os.path.isabs(source):
            source = os.path.join(request.cwd, source)
        if not os.path.exists(source):
            return FetchAttempt(candidate, 'FAILED', 'local source missing: ' + source)
        try:
            shutil.copyfile(source, request.destination)
            count, digest = self._fingerprint(request.destination)
            return FetchAttempt(candidate, 'COMPLETED', count=count, sha256=digest)
        except OSError as error:
            # Historical local_name -> shutil.copyfile raises instead of
            # trying another candidate; a partial destination may remain.
            return FetchAttempt(candidate, 'FATAL', 'local copy failed: ' + str(error))

    def _remote(self, request, candidate):
        descriptor, temporary = tempfile.mkstemp(prefix='aap-fetch-')
        os.close(descriptor)
        try:
            try:
                with urllib.request.urlopen(candidate, timeout=self.timeout) as response:
                    resolved = response.geturl()
                    if not resolved.startswith(('http://', 'https://')):
                        raise IOError('redirected to unsupported scheme: ' + resolved)
                    declared = response.info().get('Content-Length')
                    expected = int(declared) if declared and declared.isdigit() else None
                    received = 0
                    with open(temporary, 'wb') as target:
                        while True:
                            block = response.read(32768)
                            if not block:
                                break
                            target.write(block)
                            received += len(block)
                    if expected is not None and received < expected:
                        raise IOError('incomplete remote transfer: %d of %d bytes'
                                      % (received, expected))
                    with open(temporary, 'rb') as downloaded:
                        if re.search(br'<title>\s*404\s*not\s*found',
                                     downloaded.read(1000), re.I):
                            raise IOError('remote page contains a 404 title')
            except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                return FetchAttempt(candidate, 'FAILED', 'remote download failed: ' + str(error))
            try:
                shutil.copyfile(temporary, request.destination)
                count, digest = self._fingerprint(request.destination)
                return FetchAttempt(candidate, 'COMPLETED',
                                    'resolved URL: ' + resolved,
                                    count=count, sha256=digest)
            except OSError as error:
                # As in Remote.download_file, a final copy failure ends the
                # attempt and can leave partial destination bytes.
                return FetchAttempt(candidate, 'FATAL',
                                    'destination copy failed: ' + str(error))
        finally:
            os.unlink(temporary)

    def fetch(self, request):
        self.record('fetch_request', destination=request.destination,
                    candidates=list(request.candidates), cwd=request.cwd,
                    filename=request.filename, sites=request.sites)
        parent = os.path.dirname(request.destination)
        try:
            if not os.path.isdir(parent):
                os.makedirs(parent)
        except OSError as error:
            result = FetchResult('FAILED', (),
                detail='cannot create destination directory: ' + str(error))
            self.record('fetch_result', destination=request.destination,
                        status=result.status, selected=None, detail=result.detail)
            return result
        attempts = []
        for candidate in request.candidates:
            if candidate.startswith('file://'):
                attempt = self._file(request, candidate)
            elif candidate.startswith('http://') or candidate.startswith('https://'):
                attempt = self._remote(request, candidate)
            else:
                raise NotImplementedError('port fetch scheme unavailable: ' + candidate)
            attempts.append(attempt)
            self.record('fetch_attempt', candidate=candidate, status=attempt.status,
                        detail=attempt.detail, bytes=attempt.count,
                        sha256=attempt.sha256, destination=request.destination)
            if attempt.status == 'COMPLETED':
                result = FetchResult('COMPLETED', attempts, selected=candidate)
                self.record('fetch_result', destination=request.destination,
                            status=result.status, selected=candidate)
                return result
            if attempt.status == 'FATAL':
                break
        detail = (attempts[-1].detail if attempts and attempts[-1].status == 'FATAL'
                  else 'all usable fetch candidates failed')
        result = FetchResult('FAILED', attempts, detail=detail)
        self.record('fetch_result', destination=request.destination,
                    status=result.status, selected=None, detail=result.detail)
        return result
