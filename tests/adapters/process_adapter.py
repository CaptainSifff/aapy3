"""Real POSIX process execution for the bounded A-A-P process requests."""
from __future__ import print_function

import os
import shlex
import subprocess
import tempfile

from aap_semantics import ProcessBackend, ProcessResult


class PosixProcessBackend(ProcessBackend):
    def run(self, request):
        return self.run_observed(request, None)

    def run_observed(self, request, started):
        """Optional spawn callback lets an outer observer retain event order."""
        if getattr(request, 'logging', False):
            return self._run_logged(request, started)
        process = subprocess.Popen(request.shell_command_bytes, shell=True,
            executable='/bin/sh', cwd=request.cwd_bytes, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        if started is not None:
            started(process.pid, request.shell_command)
        stdout, stderr = process.communicate()
        return ProcessResult(process.returncode << 8, stdout, stderr)

    def _run_logged(self, request, started):
        """POSIX logged_system {l}: merge output, recover $? and clean temps."""
        if not request.log_path or not os.path.isabs(request.log_path):
            raise ValueError('logged :sys requires an absolute log path')
        directory = os.path.dirname(request.log_path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        output_fd, output_path = tempfile.mkstemp(prefix='py3aap-sys-output-',
                                                  dir=directory)
        os.close(output_fd)
        status_path = None
        try:
            status_fd, status_path = tempfile.mkstemp(prefix='py3aap-sys-status-',
                                                      dir=directory)
            os.close(status_fd)
            # Util.logged_system constructs this shell wrapper when {l} is
            # present. mkstemp replaces upstream's insecure tempfile.mktemp.
            command = request.command
            wrapper = ('{ ' + command + ' 2>&1; echo $? > ' + shlex.quote(status_path) +
                       '; } 2>&1 >' + shlex.quote(output_path) + '\n')
            with open(request.log_path, 'ab') as logfile:
                kind = b'log' if request.quiet else b'system'
                logfile.write(kind + b':\t' + request.log_command_text.encode('latin-1') + b'\n')
            process = subprocess.Popen(wrapper.encode('latin-1'), shell=True,
                executable='/bin/sh', cwd=request.cwd_bytes,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if started is not None:
                started(process.pid, wrapper)
            stdout, stderr = process.communicate()
            with open(status_path, 'rb') as stream:
                status_text = stream.read().strip()
            if not status_text.isdigit():
                raise ValueError('logged :sys status file has no shell status')
            with open(output_path, 'rb') as stream:
                merged = stream.read()
            if merged:
                with open(request.log_path, 'ab') as logfile:
                    logfile.write(b'log:\t' + merged + (b'' if merged.endswith(b'\n') else b'\n'))
            status = int(status_text)
            return ProcessResult(status, stdout, stderr, merged,
                                 process.returncode << 8)
        finally:
            os.unlink(output_path)
            if status_path is not None:
                os.unlink(status_path)
