"""Source-derived Commands.aap_checksum / Sign.check_md5 characterization.

No legacy driver is run. Artifacts are bytes in memory; the two authorized
recipes are the only production sources loaded by the integration fixture.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'tools'))

from aap_frontend import Source, parse
from aap_semantics import (Evaluator, Scope, lower, SemanticError, Unsupported,
    ArtifactBackend, ArtifactUnavailable, MemoryArtifacts, ChecksumBackend,
    BodyExecutor, BuildExecutionContext, UpdatePlanner, MemoryTargetState,
    ProcessBackend, ProcessResult, ProcessPolicy)
from checksum_census import census


ABC = '900150983cd24fb0d6963f7d28e17f72'
EMPTY = 'd41d8cd98f00b204e9800998ecf8427e'


def program(text, name='/recipe/main.aap'):
    return lower(parse(Source(name, text)))


class ForbiddenProcess(ProcessBackend):
    def run(self, request):
        raise AssertionError('checksum must not launch processes')


class ChecksumTests(unittest.TestCase):
    def setUp(self):
        self.artifacts = MemoryArtifacts({'/recipe/archive': b'abc'})
        self.backend = ChecksumBackend(self.artifacts)
        self.evaluator = Evaluator(checksum_backend=self.backend,
                                   process_backend=ForbiddenProcess(),
                                   process_policy=ProcessPolicy('latin-1'))

    def run_checksum(self, args):
        return self.evaluator.run(program(':checksum ' + args + '\n'))

    def failure(self, args, reason):
        with self.assertRaises(SemanticError):
            self.run_checksum(args)
        result = self.evaluator.last_result.checksums[-1]
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.reason, reason)
        return result

    def test_known_bytes_and_request_provenance(self):
        result = self.run_checksum('archive {md5 = ' + ABC + '}')
        self.assertTrue(result.complete)
        record = result.checksums[0]
        self.assertEqual((record.status, record.reason), ('VERIFIED', 'digest_match'))
        self.assertEqual(record.computed, {'md5': ABC})
        self.assertEqual(record.verified_algorithms, ('md5',))
        self.assertEqual(record.request.expected, {'md5': ABC})
        self.assertEqual(record.request.filename, 'archive')
        self.assertEqual(record.request.path, '/recipe/archive')
        self.assertEqual(record.request.cwd, '/recipe')
        self.assertEqual(record.request.span.source_id, '/recipe/main.aap')
        self.assertEqual(record.request.span.start.line, 1)
        self.assertEqual(result.processes, [])

    def test_mismatch_is_source_aware_and_retains_computed_digest(self):
        record = self.failure('archive {md5 = wrong}', 'digest_mismatch')
        self.assertEqual(record.computed, {'md5': ABC})
        self.assertEqual(record.verified_algorithms, ())
        self.assertIn('/recipe/main.aap:1:', str(record.error))
        self.assertIn('md5 checksum mismatch', str(record.error))

    def test_missing_file_is_note_not_failure_even_without_md5(self):
        result = self.run_checksum('absent')
        self.assertTrue(result.complete)
        record = result.checksums[0]
        self.assertEqual((record.status, record.reason), ('MISSING', 'artifact_missing'))
        self.assertEqual(record.computed, {})
        self.assertEqual(self.artifacts.observations, [('exists', '/recipe/absent')])

    def test_missing_file_does_not_stop_following_check(self):
        result = self.run_checksum('absent archive {md5 = ' + ABC + '}')
        self.assertEqual([r.status for r in result.checksums], ['MISSING', 'VERIFIED'])

    def test_empty_file(self):
        self.artifacts.files['/recipe/archive'] = b''
        self.assertEqual(self.run_checksum('archive {md5=' + EMPTY + '}').checksums[0].status, 'VERIFIED')

    def test_binary_bytes_are_not_decoded(self):
        # Four exact bytes; neither NUL nor non-UTF8 values pass through a codec.
        self.artifacts.files['/recipe/archive'] = b'\x00\xff\x80\n'
        result = self.run_checksum('archive {md5=1f6d00cbabcca8fc08047742afdf6cf0}')
        self.assertEqual(result.checksums[0].status, 'VERIFIED')

    def test_streaming_more_than_one_chunk(self):
        self.artifacts.files['/recipe/archive'] = b'a' * 1000000
        result = self.run_checksum('archive {md5=7707d6ae4e027c70eea2a935c2296f21}')
        self.assertEqual(result.checksums[0].status, 'VERIFIED')

    def test_uppercase_digest_is_not_normalized(self):
        self.failure('archive {md5=' + ABC.upper() + '}', 'digest_mismatch')

    def test_quoted_digest_retains_quotes_and_fails(self):
        self.failure('archive {md5="' + ABC + '"}', 'digest_mismatch')

    def test_only_space_and_tab_are_trimmed_from_digest_attribute(self):
        self.evaluator.scope.local['DIGEST'] = '\t ' + ABC + ' \t'
        self.assertTrue(self.run_checksum('archive {md5=$DIGEST}').complete)
        for suffix in ('\n', '\r', '\v', '\f', '\xa0'):
            self.evaluator.scope.local['DIGEST'] = ABC + suffix
            self.failure('archive {md5=$DIGEST}', 'digest_mismatch')

    def test_missing_md5_existing_file_fails_before_read(self):
        self.failure('archive', 'missing_md5')
        self.assertEqual(self.artifacts.observations, [('exists', '/recipe/archive')])

    def test_empty_md5_is_missing(self):
        self.failure('archive {md5 = }', 'missing_md5')

    def test_flag_md5_is_not_a_digest(self):
        self.failure('archive {md5}', 'digest_mismatch')

    def test_unknown_algorithm_alone_does_not_replace_required_md5(self):
        self.failure('archive {sha256=whatever}', 'missing_md5')

    def test_other_attributes_are_ignored_not_verified(self):
        result = self.run_checksum('archive {md5=' + ABC + '} {sha256=wrong} {unknown}')
        record = result.checksums[0]
        self.assertEqual(record.verified_algorithms, ('md5',))
        self.assertEqual(record.computed, {'md5': ABC})
        self.assertEqual(record.request.attributes['sha256'], 'wrong')

    def test_duplicate_md5_uses_last_value(self):
        self.assertTrue(self.run_checksum('archive {md5=wrong} {md5=' + ABC + '}').complete)
        self.failure('archive {md5=' + ABC + '} {md5=wrong}', 'digest_mismatch')

    def test_leading_attributes_are_not_file_attributes(self):
        self.failure('{md5=' + ABC + '} archive', 'missing_md5')

    def test_leading_attributes_are_expanded_even_when_ignored(self):
        for leading in ('{unused=$UNDEFINED}', '{unused=$UNDEFINED} {unused=ignored}'):
            with self.assertRaises(SemanticError):
                self.run_checksum(leading + ' archive {md5=' + ABC + '}')
        self.assertEqual(self.artifacts.observations, [])

    def test_production_path_and_digest_expansion(self):
        self.evaluator.scope.local.update({'DISTDIR': 'distfiles', 'DIGEST': ABC})
        self.artifacts.files['/recipe/distfiles/file.tgz'] = b'abc'
        result = self.run_checksum('$DISTDIR/file.tgz {md5 = $DIGEST}')
        self.assertEqual(result.checksums[0].request.path, '/recipe/distfiles/file.tgz')

    def test_explicit_cwd_overrides_source_directory(self):
        self.evaluator.cwd = '/build'
        self.artifacts.files['/build/archive'] = b'abc'
        result = self.run_checksum('archive {md5=' + ABC + '}')
        self.assertEqual(result.checksums[0].request.path, '/build/archive')

    def test_absolute_filename_kept(self):
        result = self.run_checksum('/recipe/archive {md5=' + ABC + '}')
        self.assertEqual(result.checksums[0].request.path, '/recipe/archive')

    def test_filename_quotes_and_literal_glob_home_characters(self):
        self.artifacts.files['/recipe/~a *?'] = b'abc'
        result = self.run_checksum('"~a *?" {md5=' + ABC + '}')
        self.assertEqual(result.checksums[0].request.path, '/recipe/~a *?')

    def test_path_dotdot_and_trailing_slash_are_not_normalized(self):
        result = self.run_checksum('a/../b/')
        self.assertEqual(result.checksums[0].request.path, '/recipe/a/../b/')

    def test_relative_path_without_cwd_blocks(self):
        with self.assertRaises(Unsupported):
            self.evaluator.run(program(':checksum archive\n', 'memory'))
        self.assertEqual(self.artifacts.observations, [])

    def test_extra_bare_field_is_another_filename(self):
        result = self.run_checksum('archive {md5=' + ABC + '} absent')
        self.assertEqual([r.request.filename for r in result.checksums], ['archive', 'absent'])

    def test_failure_stops_before_next_item(self):
        self.artifacts.files['/recipe/second'] = b'abc'
        self.failure('archive {md5=bad} second {md5=' + ABC + '}', 'digest_mismatch')
        self.assertEqual(len(self.evaluator.last_result.checksums), 1)
        self.assertEqual(self.artifacts.observations, [('exists', '/recipe/archive'), ('read', '/recipe/archive')])

    def test_directory_is_existing_but_not_hashable(self):
        self.artifacts.files['/recipe/archive'] = None  # explicit non-file fixture
        self.failure('archive {md5=' + ABC + '}', 'artifact_read_error')

    def test_read_error_after_existence_is_failure(self):
        class Disappearing(ArtifactBackend):
            def exists(self, path): return True
            def chunks(self, path): raise OSError('file disappeared')
        self.evaluator = Evaluator(checksum_backend=ChecksumBackend(Disappearing()))
        self.failure('archive {md5=' + ABC + '}', 'artifact_read_error')

    def test_text_backend_data_is_rejected(self):
        class TextData(ArtifactBackend):
            def exists(self, path): return True
            def chunks(self, path): return ['abc']
        self.evaluator = Evaluator(checksum_backend=ChecksumBackend(TextData()))
        self.failure('archive {md5=' + ABC + '}', 'artifact_read_error')

    def test_invalid_digest_observation_is_backend_error(self):
        class Invalid(ChecksumBackend):
            def md5(self, request): return 'unknown'
        self.evaluator = Evaluator(checksum_backend=Invalid(self.artifacts))
        self.failure('archive {md5=' + ABC + '}', 'artifact_read_error')

    def test_hash_provider_error_is_source_aware_failure(self):
        class Unusable(ChecksumBackend):
            def md5(self, request): raise ValueError('MD5 provider unavailable')
        self.evaluator = Evaluator(checksum_backend=Unusable(self.artifacts))
        record = self.failure('archive {md5=' + ABC + '}', 'artifact_read_error')
        self.assertIn('/recipe/main.aap:1:', str(record.error))

    def test_no_argument_and_malformed_attributes_fail_before_io(self):
        for args in ('', 'archive {md5=', 'archive {=bad}', 'archive {md5 nope}'):
            with self.assertRaises(SemanticError):
                self.run_checksum(args)
        self.assertEqual(self.artifacts.observations, [])

    def test_unsupported_expansion_blocks_without_io(self):
        self.evaluator.scope.local['FILES'] = ['archive']
        with self.assertRaises(Unsupported):
            self.run_checksum('$FILES {md5=' + ABC + '}')
        self.assertEqual(self.artifacts.observations, [])

    def test_unsupported_forms_block_before_io(self):
        for text in (':checksum archive | :assign x\n',
                     ':checksum `unknown()` {md5=bad}\n',
                     ':checksum archive {md5={nested}}\n'):
            with self.assertRaises(Unsupported):
                self.evaluator.run(program(text))
        self.assertEqual(self.artifacts.observations, [])

    def test_indented_argument_continuation_uses_existing_frontend(self):
        result = self.evaluator.run(program(':checksum archive\n  {md5=' + ABC + '}\n'))
        self.assertEqual(result.checksums[0].status, 'VERIFIED')

    def test_generated_census_is_one_bounded_form(self):
        report = census(os.path.join(ROOT, 'tests', 'fixtures', 'command-forms'))
        self.assertEqual(report['occurrences'], 829)
        self.assertEqual(report['matched_single_path_then_one_md5_attribute'], 829)
        self.assertEqual(report['digest_lengths'], {'32': 829})
        self.assertEqual(report['variables'], {'DISTDIR': 807, 'PATCHDISTDIR': 22})
        self.assertEqual(report['exceptions'], [])


class ChecksumBodyTests(unittest.TestCase):
    def context(self, body, backend):
        metadata = Evaluator().run(program('all:\n' + body))
        plan = UpdatePlanner(metadata.graph, MemoryTargetState(), metadata.scope).plan('all')
        return BuildExecutionContext(plan.bodies[0], metadata.scope, metadata.graph,
                                     metadata.declarations, checksum_backend=backend,
                                     process_backend=ForbiddenProcess(),
                                     process_policy=ProcessPolicy('latin-1'))

    def test_success_continues_without_graph_or_update_state_mutation(self):
        artifacts = MemoryArtifacts({'/recipe/archive': b'abc'})
        context = self.context('  :checksum archive {md5=' + ABC + '}\n  AFTER = yes\n',
                               ChecksumBackend(artifacts))
        before = context.graph.snapshot()
        result = BodyExecutor().execute(context)
        self.assertEqual(result.status, 'COMPLETED')
        self.assertEqual(result.scope.local['AFTER'], 'yes')
        self.assertEqual(result.checksums[0].status, 'VERIFIED')
        self.assertEqual(context.graph.snapshot(), before)
        self.assertFalse(result.graph_changed)
        self.assertFalse(result.declarations_changed)
        self.assertFalse(result.target_updated)
        self.assertFalse(result.persistence_performed)
        self.assertTrue(result.post_execution_recheck_required)
        self.assertEqual(artifacts.files, {'/recipe/archive': b'abc'})
        self.assertEqual(result.processes, ())

    def test_mismatch_fails_without_following_assignment(self):
        context = self.context('  BEFORE = yes\n  :checksum archive {md5=bad}\n  AFTER = no\n',
                               ChecksumBackend(MemoryArtifacts({'/recipe/archive': b'abc'})))
        result = BodyExecutor().execute(context)
        self.assertEqual(result.status, 'FAILED')
        self.assertEqual(result.scope.local['BEFORE'], 'yes')
        self.assertNotIn('AFTER', result.scope.local)
        self.assertEqual(result.span.start.line, 3)
        self.assertEqual(result.checksums[0].reason, 'digest_mismatch')
        self.assertFalse(result.target_updated)

    def test_missing_artifact_completes_without_fetch(self):
        context = self.context('  :checksum absent\n  AFTER = yes\n', ChecksumBackend(MemoryArtifacts()))
        result = BodyExecutor().execute(context)
        self.assertEqual(result.status, 'COMPLETED')
        self.assertEqual(result.checksums[0].status, 'MISSING')
        self.assertEqual(result.scope.local['AFTER'], 'yes')

    def test_unavailable_capability_blocks(self):
        context = self.context('  :checksum archive {md5=' + ABC + '}\n  AFTER = no\n',
                               ChecksumBackend(ArtifactBackend()))
        result = BodyExecutor().execute(context)
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.blocked_at.name, 'checksum')
        self.assertEqual(result.checksums[0].reason, 'artifact_capability_unavailable')
        self.assertNotIn('AFTER', result.scope.local)

    def test_unavailable_read_after_exists_blocks(self):
        class NoRead(ArtifactBackend):
            def exists(self, path): return True
            def chunks(self, path): raise ArtifactUnavailable('read capability absent')
        context = self.context('  :checksum archive {md5=' + ABC + '}\n',
                               ChecksumBackend(NoRead()))
        result = BodyExecutor().execute(context)
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.checksums[0].reason, 'artifact_capability_unavailable')

    def test_no_backend_preserves_inert_command_barrier(self):
        result = BodyExecutor().execute(self.context('  :checksum $UNDEFINED\n', None))
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.blocked_at.name, 'checksum')
        self.assertEqual(result.checksums, ())

    def test_next_unsupported_command_still_blocks_without_argument_effects(self):
        context = self.context('  :checksum archive {md5=' + ABC + '}\n'
                               '  :sys $UNDEFINED\n  AFTER = no\n',
                               ChecksumBackend(MemoryArtifacts({'/recipe/archive': b'abc'})))
        result = BodyExecutor().execute(context)
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual(result.blocked_at.name, 'sys')
        self.assertEqual(result.checksums[0].status, 'VERIFIED')
        self.assertNotIn('AFTER', result.scope.local)
        self.assertEqual(result.processes, ())

    def test_nano_checksum_reaches_body_eof_with_controlled_digest_observation(self):
        class FakeProcess(ProcessBackend):
            def __init__(self): self.requests = []
            def run(self, request):
                output = (b'SUSE15\n', b'suse\n', b'SUSE 15 6\n')[len(self.requests)]
                self.requests.append(request)
                return ProcessResult(0, output)
        root = '/authorized'
        nano = root + '/ports/editors/nano/main.aap'
        globals_source = Source(root + '/ports/globals.aap', Source.from_path(
            os.path.join(ROOT, 'tests/fixtures/ports/globals.aap'), 'latin-1').text)
        class Loader(object):
            def load(self, path):
                if path != globals_source.source_id:
                    raise AssertionError('unauthorized source: ' + path)
                return globals_source
        archive = root + '/ports/editors/nano/distfiles/nano-7.1.tar.gz'
        # Explicit test oracle: synthetic bytes are NOT claimed to have the
        # archive's real digest. Normal verification is tested above and below.
        class RecordedDigest(ChecksumBackend):
            def md5(self, request):
                if request.path != archive:
                    raise AssertionError('unexpected digest request')
                return 'cc9e42c4805193f9dc3ae22b9644bb1d'
        artifacts = MemoryArtifacts({archive: b'synthetic integration artifact'})
        scope = Scope.top_level()
        scope.local.update({'OSNAME': 'Linux', 'BDIR': 'build', 'DISTDIR': 'distfiles',
                            'PKGDIR': root + '/ports/editors/nano/pack'})
        process = FakeProcess()
        metadata = Evaluator(scope, include_loader=Loader(), process_backend=process,
                             process_policy=ProcessPolicy('latin-1')).run(program(
            Source.from_path(os.path.join(ROOT, 'tests/fixtures/ports/editors/nano/main.aap'), 'latin-1').text, nano))
        self.assertTrue(metadata.complete)
        self.assertEqual((len(metadata.graph.definitions), len(metadata.graph.targets),
                          len(metadata.graph.nodes)), (35, 35, 39))
        planner = UpdatePlanner(metadata.graph, MemoryTargetState(), scope)
        self.assertEqual(planner.plan().requests, ())
        plan = planner.plan('do-checksum')
        self.assertEqual(tuple(e.target.name for e in plan.entries), ('do-checksum',))
        def enter(backend):
            return BodyExecutor().execute(BuildExecutionContext(
                plan.bodies[0], scope, metadata.graph, metadata.declarations,
                process_backend=process, process_policy=ProcessPolicy('latin-1'),
                prepared_buildcheck='controlled prepared signature observation',
                checksum_backend=backend))
        failed = enter(ChecksumBackend(artifacts))
        self.assertEqual(failed.status, 'FAILED')
        self.assertEqual(failed.checksums[0].reason, 'digest_mismatch')
        result = enter(RecordedDigest(artifacts))
        self.assertEqual(result.status, 'COMPLETED')
        self.assertIsNone(result.blocked_at)
        self.assertEqual(result.checksums[0].status, 'VERIFIED')
        request = result.checksums[0].request
        self.assertEqual(request.expected, {'md5': 'cc9e42c4805193f9dc3ae22b9644bb1d'})
        self.assertEqual(request.path, archive)
        self.assertEqual(request.span.source_id, nano)
        self.assertEqual(request.span.start.line, 51)
        self.assertEqual(result.context.cwd, root + '/ports/editors/nano')
        self.assertEqual(result.scope.local['target_list'], ['do-checksum'])
        self.assertEqual(result.scope.local['source_list'], [])
        self.assertEqual(result.scope.local['depend_list'], [])
        self.assertEqual(len(result.program.statements), 1)  # EOF, no next command
        self.assertTrue(result.post_execution_recheck_required)
        self.assertFalse(result.target_updated)
        self.assertFalse(result.persistence_performed)
        self.assertFalse(result.graph_changed)
        self.assertEqual(len(process.requests), 3)  # metadata only
        self.assertEqual(result.processes, ())
        self.assertEqual(planner.plan('fake-install').diagnostic.code, 'no_build_commands')


if __name__ == '__main__':
    unittest.main()
