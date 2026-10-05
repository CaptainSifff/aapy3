#!/usr/bin/env python3
"""Disposable generic CLI adapter for real recursive A-A-P integration.

The adapter deliberately discovers ``main.aap`` from the process cwd.  It is
not a recursive interpreter feature: every ``aap`` command is launched by the
ordinary process backend and starts this program in a new OS process.
"""
from __future__ import print_function

import os
import sys


ROOT = os.environ.get('AAP_REPOSITORY', '/work/repo')
sys.path.insert(0, os.path.join(ROOT, 'tests', 'src'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'adapters'))

from aap_frontend import Source, parse
from aap_semantics import (BuildDriver, CliArgumentError, Evaluator,
                           apply_assignments, lower, parse_arguments)
from host_filesystem import Files  # public integration fixture import
from integration_evidence import (EventRecorder, body_cat_records,
    body_decisions, body_directory_changes, body_print_records,
    body_process_captures, body_tree_records, completion_decisions,
    inventory, python_write_records, span)
from process_adapter import PosixProcessBackend as Shell  # fixture import
from runtime_factory import create_runtime, initial_scope


def main(argv):
    environment = os.environ
    record = EventRecorder(environment)
    cwd = os.path.realpath(os.getcwd())
    recipe = os.path.join(cwd, 'main.aap')
    recipe_aap = environment.get('AAP')
    if not os.path.isfile(recipe):
        record('cli_error', cwd=cwd, argv=argv,
                     category='recipe discovery', detail='main.aap is absent')
        print('aap: main.aap is absent in ' + cwd, file=sys.stderr)
        return 2
    try:
        parsed = parse_arguments(argv)
    except CliArgumentError as error:
        record('cli_error', cwd=cwd, argv=argv, category='CLI/entrypoint',
                     detail=str(error))
        print('aap: ' + str(error), file=sys.stderr)
        return 2
    targets = parsed.targets or None
    if not recipe_aap:
        record('cli_error', cwd=cwd, argv=argv, category='launcher',
                     detail='AAP launcher value is absent')
        print('aap: AAP launcher value is absent', file=sys.stderr)
        return 2

    record('cli_start', cwd=cwd, argv=argv, recipe=recipe,
                 top_recipe='main.aap', source_identity=recipe,
                 sentinel=environment.get('AAP_RECURSIVE_SENTINEL'),
                 path=environment.get('PATH'), recipe_aap=recipe_aap,
                 python_executable=sys.executable, entrypoint=sys.argv[0],
                 environment={key: environment.get(key) for key in
                              ('AAP', 'AAP_REPOSITORY', 'AAP_RECURSIVE_SENTINEL',
                               'AAP_RECURSIVE_INVOCATION', 'PATH')})
    scope = initial_scope(cwd, recipe_aap)
    applied_assignments, ignored_assignments = apply_assignments(
        scope, parsed.assignments)
    record('cli_settings', cwd=cwd, argv=argv,
                 assignments=parsed.assignments,
                 applied_assignments=applied_assignments,
                 ignored_assignments=ignored_assignments,
                 targets=parsed.targets)
    runtime = create_runtime(cwd, environment, record, scope)
    source = Source.from_path(recipe, 'latin-1')
    metadata = Evaluator(scope, cwd=cwd,
        capabilities=runtime.metadata_capabilities).run(lower(parse(source)))
    if not metadata.complete:
        record('cli_metadata_exit', cwd=cwd, argv=argv, status='FAILED',
                     halted_at=span(metadata.halted_at.span if metadata.halted_at else None))
        return 1

    driver = BuildDriver(metadata.graph, runtime.state, runtime.persistence,
        scope, metadata.declarations, cwd=cwd,
        capabilities=runtime.capabilities)
    result = driver.build(targets)
    finish = driver.finish()
    exit_code = 0 if result.status == 'COMPLETE' and finish.status == 'COMPLETE' else 1
    record('cli_exit', cwd=cwd, argv=argv, recipe=recipe,
                 top_recipe='main.aap', status=result.status,
                 reason=result.reason, error=str(result.error) if result.error else None,
                 span=span(result.span), bodies=[body.target.name for body in result.bodies],
                 pending_signatures=len(result.pending_signatures),
                 pending_targets=[item.target for item in result.pending_signatures],
                 body_decisions=body_decisions(result),
                 completion_decisions=completion_decisions(result),
                 marker_observations=(list(driver.port.marker_observations)
                                      if driver.port is not None else []),
                 finish_status=finish.status,
                 persistence_written=finish.persistence_written,
                 inventory=inventory(cwd, environment), exit_code=exit_code,
                 recipe_aap=recipe_aap,
                 sentinel=environment.get('AAP_RECURSIVE_SENTINEL'),
                 tree_records=body_tree_records(result),
                 directory_changes=body_directory_changes(result),
                 cats=body_cat_records(result),
                 print_records=body_print_records(result),
                 process_captures=body_process_captures(result),
                 python_writes=python_write_records(result))
    if exit_code:
        print('aap: {0}: {1}: {2}'.format(result.status, result.reason,
              str(result.error) if result.error else ''), file=sys.stderr)
    return exit_code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
