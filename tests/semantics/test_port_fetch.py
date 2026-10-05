"""Source-derived port_fetch ordering with a network-free acquisition backend."""
import hashlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))

from aap_frontend import Source, parse
from aap_semantics import (BuildDriver, ChecksumBackend, Evaluator, FetchBackend,
    FetchResult, MemoryArtifacts, MemoryFetchBackend, MemoryMarkers,
    MemoryPersistence, MemoryTargetState, PortRuntime, Scope, lower)


class PortFetchTests(unittest.TestCase):
    def make(self, sources=None, variables=None, files=None, fetch_backend=None,
             checksum=False):
        scope = Scope.top_level()
        scope.local.update({'DISTFILES': 'path/archive.tgz', 'DISTDIR': 'dist',
                            'MASTER_SITES': 'file://missing https://one.test/base '
                                            'https://two.test/base',
                            'PATCHFILES': '', 'PATCH_SITES': 'file://files',
                            'PATCHDISTDIR': 'patches'})
        scope.local.update(variables or {})
        artifacts = MemoryArtifacts(files if files is not None else {}, shared=True)
        markers = MemoryMarkers()
        fetch = fetch_backend if fetch_backend is not None else MemoryFetchBackend(
            artifacts.files, sources)
        runtime = PortRuntime(artifacts, markers, fetch_backend=fetch)
        body = '  @port_fetch(globals())\n'
        if checksum:
            body += '  :checksum dist/archive.tgz {md5 = %s}\n' % checksum
        data = Evaluator(scope).run(lower(parse(Source(
            '/recipe/main.aap', 'all:\n' + body))))
        driver = BuildDriver(data.graph, MemoryTargetState(), MemoryPersistence(),
                             scope, data.declarations, port_runtime=runtime,
                             checksum_backend=ChecksumBackend(artifacts))
        return driver, runtime, fetch, artifacts, markers

    def test_existing_destination_short_circuits_candidates(self):
        driver, runtime, fetch, artifacts, markers = self.make(
            files={'/recipe/dist/archive.tgz': b'old'})
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(fetch.requests, [])
        self.assertEqual(fetch.directories, [])
        self.assertEqual(artifacts.files['/recipe/dist/archive.tgz'], b'old')
        self.assertEqual(markers.files['/recipe/done/cvs-no'], b'')

    def test_ordered_three_site_fallback_and_basename(self):
        selected = 'https://two.test/base/path/archive.tgz'
        driver, runtime, fetch, artifacts, markers = self.make(
            sources={selected: b'payload'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE', str(result.error))
        request, outcome = result.bodies[0].port_operations[0].fetches[0]
        self.assertEqual(request.destination, '/recipe/dist/archive.tgz')
        self.assertEqual(request.candidates, (
            'file://missing/path/archive.tgz',
            'https://one.test/base/path/archive.tgz', selected))
        self.assertEqual([a.candidate for a in outcome.attempts],
                         list(request.candidates))
        self.assertEqual([a.status for a in outcome.attempts],
                         ['FAILED', 'FAILED', 'COMPLETED'])
        self.assertEqual(outcome.selected, selected)
        self.assertEqual(artifacts.files[request.destination], b'payload')
        self.assertEqual(fetch.directories, ['/recipe/dist'])
        self.assertIn('/recipe/done/cvs-no', markers.files)
        self.assertNotIn('/recipe/done/fetch', markers.files)
        self.assertEqual(result.bodies[0].scope.local['EXTRACTFILES'],
                         'path/archive.tgz')

    def test_first_success_stops_before_later_candidates(self):
        first = 'file://missing/path/archive.tgz'
        second = 'https://one.test/base/path/archive.tgz'
        for selected, expected_count in ((first, 1), (second, 2)):
            driver, runtime, fetch, artifacts, markers = self.make(
                sources={selected: b'ok'})
            result = driver.build('all')
            self.assertEqual(result.status, 'COMPLETE')
            request, outcome = result.bodies[0].port_operations[0].fetches[0]
            self.assertEqual(len(fetch.requests), 1)
            self.assertEqual(len(outcome.attempts), expected_count)
            self.assertEqual(outcome.selected, selected)
            self.assertEqual(len(artifacts.files[request.destination]), 2)

    def test_all_candidates_fail_without_marker(self):
        driver, runtime, fetch, artifacts, markers = self.make()
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        operation = result.bodies[0].port_operations[0]
        self.assertEqual(operation.status, 'FAILED')
        self.assertEqual(len(operation.fetches[0][1].attempts), 3)
        self.assertNotIn('/recipe/dist/archive.tgz', artifacts.files)
        self.assertEqual(markers.files, {})
        self.assertEqual(fetch.directories, ['/recipe/dist'])

    def test_patch_uses_same_backend_after_distfile(self):
        driver, runtime, fetch, artifacts, markers = self.make(
            variables={'PATCHFILES': 'fix.diff'},
            files={'/recipe/dist/archive.tgz': b'old'},
            sources={'file://files/fix.diff': b'patch'})
        result = driver.build('all')
        self.assertEqual(result.status, 'COMPLETE', str(result.error))
        self.assertEqual(fetch.requests[0].destination, '/recipe/patches/fix.diff')
        self.assertEqual(fetch.requests[0].candidates, ('file://files/fix.diff',))
        self.assertEqual(artifacts.files['/recipe/patches/fix.diff'], b'patch')

    def test_existing_patch_short_circuits_backend(self):
        driver, runtime, fetch, artifacts, markers = self.make(
            variables={'PATCHFILES': 'fix.diff'},
            files={'/recipe/dist/archive.tgz': b'archive',
                   '/recipe/patches/fix.diff': b'patch'})
        self.assertEqual(driver.build('all').status, 'COMPLETE')
        self.assertEqual(fetch.requests, [])

    def test_unavailable_backend_blocks(self):
        driver, runtime, fetch, artifacts, markers = self.make(
            fetch_backend=FetchBackend())
        self.assertEqual(driver.build('all').status, 'BLOCKED')
        self.assertEqual(markers.files, {})

    def test_backend_success_without_observed_artifact_fails(self):
        class LyingFetch(FetchBackend):
            def fetch(self, request):
                return FetchResult('COMPLETED', (), selected=request.candidates[0])
        driver, runtime, fetch, artifacts, markers = self.make(
            fetch_backend=LyingFetch())
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        self.assertIn('success without artifact', str(result.error))
        self.assertEqual(markers.files, {})

    def test_generated_fetch_marker_follows_successful_acquisition(self):
        scope = Scope.top_level(port_defaults=True)
        scope.local.update({'PORTNAME': 'fixture', 'PORTVERSION': '1',
                            'PORTCOMMENT': 'fixture', 'PORTDESCR': 'fixture',
                            'DISTFILES': 'archive.tgz', 'PATCHFILES': '',
                            'MASTER_SITES': 'file://files', 'PATCH_SITES': '',
                            'DISTDIR': 'dist', 'PATCHDISTDIR': 'patches',
                            'WRKDIR': 'work', 'PKGDIR': 'pack'})
        data = Evaluator(scope).run(lower(parse(Source('/recipe/main.aap',
            'do-dependcheck:\n  :pass\ndo-fetchdepend:\n  :pass\n'))))
        artifacts = MemoryArtifacts({}, shared=True)
        markers = MemoryMarkers()
        fetch = MemoryFetchBackend(artifacts.files,
                                   {'file://files/archive.tgz': b'bytes'})
        runtime = PortRuntime(artifacts, markers, fetch_backend=fetch)
        driver = BuildDriver(data.graph, MemoryTargetState(), MemoryPersistence(),
                             scope, data.declarations, port_runtime=runtime)
        result = driver.build('fetch')
        self.assertEqual(result.status, 'COMPLETE', str(result.error))
        self.assertIn('/recipe/done/cvs-no', markers.files)
        self.assertIn('/recipe/done/fetch', markers.files)
        self.assertEqual(artifacts.files['/recipe/dist/archive.tgz'], b'bytes')

    def test_fetch_success_does_not_certify_checksum(self):
        expected = hashlib.md5(b'correct').hexdigest()
        candidate = 'file://missing/path/archive.tgz'
        driver, runtime, fetch, artifacts, markers = self.make(
            sources={candidate: b'wrong'}, checksum=expected)
        result = driver.build('all')
        self.assertEqual(result.status, 'FAILED')
        operation = result.bodies[0].port_operations[0]
        self.assertEqual(operation.status, 'COMPLETED')
        self.assertEqual(operation.fetches[0][1].selected, candidate)
        self.assertEqual(artifacts.files['/recipe/dist/archive.tgz'], b'wrong')
        self.assertIn('/recipe/done/cvs-no', markers.files)


if __name__ == '__main__':
    unittest.main()
