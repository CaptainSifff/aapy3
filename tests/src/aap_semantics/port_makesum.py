"""Linux Port.py:248-380 semantics, including unreachable restoration code."""
import posixpath

from .checksum import ChecksumBackend, ChecksumRequest, ArtifactBackend
from .command_items import items
from .diagnostics import SemanticError, Unsupported
from .recipe_mutation import RecipeMutationRequest, RecipeMutationResult
from .values import MISSING


START = b'#>>> automatically inserted by "aap makesum" <<<\n'
END = b'#>>> end <<<\n'


def makesum(port, record, evaluator):
    work = record.scope.get_work()
    if work is None or not work.top_recipe:
        raise SemanticError(record, 'No recipe specified to makesum for')
    if type(work.top_recipe) is not str:
        raise Unsupported(record, 'top recipe identity must be a path string')
    recipe = port._path(record.cwd, work.top_recipe, record)
    if evaluator is None:
        raise Unsupported(record, 'makesum requires path observations')
    paths = evaluator.python.helpers.paths

    def exists(argument):
        before = len(paths.records)
        try:
            return paths.exists(argument, record.cwd, record)
        finally:
            if len(paths.records) > before:
                record.observations.append(paths.records[-1])

    # Deliberately construct the concrete byte-hashing implementation. The
    # verification backend may be a RecordedChecksum oracle; never use it here.
    digest = ChecksumBackend(port.artifacts if port.artifacts is not None else ArtifactBackend())
    lines = []
    for variable, directory in (('DISTFILES', 'DISTDIR'), ('PATCHFILES', 'PATCHDISTDIR')):
        files = []
        for name in (variable, 'CVS' + variable):
            for filename, unused in items(port._value(record.scope, name, record), record):
                basename = posixpath.basename(filename)
                if basename not in files:
                    files.append(basename)
        for filename in files:
            value = record.scope.lookup(directory)
            if value is MISSING or value is None:
                raise SemanticError(record, 'missing makesum directory: ' + directory)
            directory_value = port._value(record.scope, directory, record)
            argument = posixpath.join(directory_value, filename)
            request = ChecksumRequest(record, argument, {}, record.cwd)
            record.paths.append(request.path)
            if not exists(argument):
                raise SemanticError(record, 'File does not exists: "' + argument + '"')
            try:
                checksum = digest.md5(request)
            except OSError as error:
                raise SemanticError(record, 'Cannot compute checksum for "' + argument + '": ' + str(error))
            record.checksums.append((request, checksum))
            # ASCII filename spelling is the bounded production surface. Recipe
            # contents themselves are never decoded, even for unrelated bytes.
            try:
                line = ('\t:checksum $%s/%s {md5 = %s}\n' %
                        (directory, filename, checksum)).encode('ascii')
            except UnicodeError:
                raise Unsupported(record, 'non-ASCII makesum filename encoding is deferred')
            lines.append(line)
    _RecipeEditor(port.recipe_mutations, record, exists).rewrite(
        recipe, work.top_recipe, lines)


class _RecipeEditor(object):
    def __init__(self, backend, record, exists):
        self.backend, self.record, self.exists = backend, record, exists

    def effect(self, operation, path, destination=None, data=None):
        request = RecipeMutationRequest(self.record, operation, path, destination, data)
        try:
            result = self.backend.apply(request)
        except NotImplementedError as error:
            result = RecipeMutationResult('UNAVAILABLE', str(error))
        except OSError as error:
            result = RecipeMutationResult('FAILED', str(error))
        self.record.mutations.append((request, result))
        if not isinstance(result, RecipeMutationResult):
            raise OSError('invalid recipe mutation result')
        if result.status == 'UNAVAILABLE':
            raise Unsupported(self.record, result.detail or 'recipe mutation unavailable')
        if result.status != 'COMPLETED':
            raise OSError(result.detail or 'recipe mutation failed')
        if operation == 'readline' and type(result.data) is not bytes:
            raise OSError('recipe reader must return exact bytes')
        return result.data

    def try_delete(self, path):
        try:
            self.effect('remove', path)
        except OSError:
            pass  # Util.try_delete suppresses actual removal failures.
        # Unavailable capability is not a historical I/O failure; keep it BLOCKED.

    def error(self, message, error):
        raise SemanticError(self.record, message + str(error))

    def rewrite(self, recipe, spelling, lines):
        try:
            self.effect('open_read', recipe)
        except OSError as error:
            self.error('Cannot open recipe file "%s": ' % spelling, error)
        number = 1
        while self.exists(recipe + str(number)):
            number += 1
        temp = recipe + str(number)
        try:
            self.effect('create_temp', temp)
        except OSError as error:
            self.error('Cannot create temp file "%s": ' % (spelling + str(number)), error)

        def write(data):
            self.effect('write', temp, data=data)

        def checksum_lines():
            write(b'do-checksum:\n')
            for line in lines or [b'\t@pass\n']:
                write(line)
            write(END)

        try:
            added = False
            while True:
                line = self.effect('readline', recipe)
                if not line:
                    break
                write(line)
                if line == START:
                    if added:
                        raise SemanticError(self.record, 'Duplicate makesum start marker')
                    added = True
                    checksum_lines()
                    while True:
                        line = self.effect('readline', recipe)
                        if not line:
                            raise SemanticError(self.record, 'Missing makesum end marker')
                        if line == END:
                            break
            if not added:
                write(START)
                checksum_lines()
            self.effect('close_read', recipe)
            self.effect('close_temp', temp)
        except Unsupported:
            # Capability absence is not evidence of an OS copying failure.
            # Preserve the last observed state and report BLOCKED.
            raise
        except (OSError, SemanticError) as error:
            try:
                self.effect('close_temp', temp)
            except OSError:
                pass
            self.effect('remove', temp)  # unlike try_delete, can mask copy error
            self.error('Error while copying recipe file: ', error)

        backup = recipe + '~'
        if self.exists(backup):
            try:
                self.effect('remove', backup)
            except OSError as error:
                self.try_delete(temp)
                self.error('Cannot delete backup recipe "%s": ' % (spelling + '~'), error)
        try:
            self.effect('rename', recipe, backup)
        except OSError as error:
            self.try_delete(temp)
            self.error('Cannot rename recipe "%s" to "%s": ' %
                       (spelling, spelling + '~'), error)
        try:
            self.effect('rename', temp, recipe)
        except OSError as error:
            # Port.py:371 calls recipe_error (always raises). Its subsequent
            # backup -> original restoration is unreachable. Preserve that bug.
            self.error('Cannot rename recipe to "%s": ' % spelling, error)
        self.try_delete(temp)
