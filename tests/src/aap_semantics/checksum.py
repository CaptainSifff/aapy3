"""Read-only package MD5 verification, independent of build signatures.

Evidence: Commands.aap_checksum/get_args, Dictlist.str2dictlist/parse_attr,
Sign.check_md5/hexdigest. No local filesystem adapter is implicitly enabled.
"""
import hashlib
import posixpath

from .model import Node, PythonFragment
from .diagnostics import SemanticError, Unsupported
from .expansion import render_value, expand_text
from .command_items import attributes, items


class ArtifactUnavailable(Exception):
    """The caller has not supplied a required read capability."""


class ArtifactBackend(object):
    """Read-only capability. Paths are explicit absolute POSIX paths.

    exists must distinguish absence from an existing directory/unreadable file.
    chunks yields exact bytes from a regular file or raises OSError. A missing
    file after a successful exists probe is a read error, not a skipped check.
    No write, process, graph or signature operation belongs to this interface.
    """
    def exists(self, path):
        raise ArtifactUnavailable('artifact existence observation unavailable')

    def chunks(self, path):
        raise ArtifactUnavailable('artifact byte reads unavailable')


class MemoryArtifacts(ArtifactBackend):
    """In-memory regular files for controlled semantic tests; records reads."""
    def __init__(self, files=None, shared=False):
        # Explicit fixture adapter for a live MemoryTextWriter byte store.
        self.files = files if shared and files is not None else dict(files or {})
        self.observations = []

    def exists(self, path):
        self.observations.append(('exists', path))
        return path in self.files

    def chunks(self, path):
        self.observations.append(('read', path))
        if path not in self.files:
            raise OSError('artifact disappeared: ' + path)
        data = self.files[path]
        if type(data) is not bytes:
            raise OSError('artifact is not a regular byte file: ' + path)
        for index in range(0, len(data), 32768):
            yield data[index:index + 32768]


class ChecksumBackend(object):
    """Digest observation seam. Default implementation hashes exact bytes.

    Controlled integration tests may override md5 with a recorded observation;
    comparison remains in ChecksumRuntime and is never disabled. A real adapter
    must use the supplied read-only artifacts (no recipe-controlled callbacks).
    """
    def __init__(self, artifacts):
        self.artifacts = artifacts

    def exists(self, request):
        return self.artifacts.exists(request.path)

    def md5(self, request):
        digest = hashlib.md5()
        for chunk in self.artifacts.chunks(request.path):
            if type(chunk) is not bytes:
                raise OSError('artifact backend must yield bytes')
            digest.update(chunk)
        return digest.hexdigest()


class ChecksumRequest(Node):
    def __init__(self, origin, filename, attributes, cwd):
        super(ChecksumRequest, self).__init__(origin)
        self.filename = filename
        self.attributes = dict(attributes)
        self.expected = {'md5': attributes['md5']} if 'md5' in attributes else {}
        self.cwd = cwd
        if '\x00' in filename:
            raise SemanticError(origin, 'NUL in checksum filename')
        if not posixpath.isabs(filename):
            if cwd is None or not posixpath.isabs(cwd):
                raise Unsupported(origin, 'relative checksum path requires an explicit absolute cwd')
            filename = posixpath.join(cwd, filename)
        # No glob, home expansion, alias/search lookup or normpath: preserving
        # a/../b and trailing slash matters for symlinks and directory checks.
        self.path = filename


class ChecksumResult(object):
    def __init__(self, request):
        self.request = request
        self.status = None  # VERIFIED, MISSING, FAILED, BLOCKED
        self.reason = None
        self.computed = {}
        self.verified_algorithms = ()
        self.error = None


class ChecksumRuntime(object):
    def __init__(self, backend):
        self.backend = backend

    def verify(self, command, scope, python, cwd, records):
        if command.body is not None:
            raise Unsupported(command, 'checksum bodies are outside the production subset')
        if any(isinstance(part, PythonFragment) for piece in command.arguments.pieces for part in piece):
            raise Unsupported(command, 'checksum backticks are outside the production subset')
        raw = render_value(command.arguments, python)
        if '|' in raw:
            raise Unsupported(command, 'checksum pipelines are outside the production subset')
        leading, index = attributes(raw, 0, command, leading_scope=scope, label="checksum")
        text = expand_text(raw[index:], scope, command, item_attributes=True)
        parsed = items(text, command, label="checksum")
        if not parsed:
            raise SemanticError(command, ':checksum requires a file argument')
        for filename, attrs in parsed:
            request = ChecksumRequest(command, filename, attrs, cwd)
            result = ChecksumResult(request)
            records.append(result)  # preserve observations even on failure
            try:
                exists = self.backend.exists(request)
                if type(exists) is not bool:
                    raise OSError('artifact existence observation must be bool')
                if not exists:
                    result.status, result.reason = 'MISSING', 'artifact_missing'
                    continue  # historical note; no digest validation or fetch
                if not attrs.get('md5'):
                    self.fail(result, 'missing_md5', 'md5 attribute missing')
                computed = self.backend.md5(request)
                if (type(computed) is not str or len(computed) != 32
                        or any(c not in '0123456789abcdef' for c in computed)):
                    raise OSError('invalid MD5 observation from checksum backend')
                result.computed['md5'] = computed
                if computed != attrs['md5']:
                    self.fail(result, 'digest_mismatch', 'md5 checksum mismatch')
                result.status, result.reason = 'VERIFIED', 'digest_match'
                result.verified_algorithms = ('md5',)
            except ArtifactUnavailable as error:
                result.status, result.reason = 'BLOCKED', 'artifact_capability_unavailable'
                result.error = Unsupported(command, str(error))
                raise result.error
            except SemanticError:
                # FrontendError derives from ValueError; do not reclassify an
                # intentional mismatch/missing-digest failure as a read error.
                raise
            except (OSError, ValueError) as error:
                self.fail(result, 'artifact_read_error', 'cannot compute md5 checksum: ' + str(error))

    @staticmethod
    def fail(result, reason, message):
        result.status, result.reason = 'FAILED', reason
        result.error = SemanticError(result.request, message + ': ' + result.request.filename)
        raise result.error
