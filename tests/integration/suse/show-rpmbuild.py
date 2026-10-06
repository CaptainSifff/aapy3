"""Print the real rpmbuild request and captured output in the Actions log."""
from __future__ import print_function

import json
import sys


def main(path):
    matches = []
    with open(path, 'r') as stream:
        for line in stream:
            event = json.loads(line)
            if event.get('event') != 'process_exit':
                continue
            command = event.get('command') or ''
            if 'rpmbuild' in command:
                matches.append((command, event))
    if not matches:
        raise SystemExit('A-A-P trace contains no rpmbuild process')
    for command, event in matches:
        print('$ ' + command)
        print('exit status: {0}'.format(event.get('returncode')))
        for key in ('stdout', 'stderr'):
            value = event.get(key) or ''
            print('--- {0} ---'.format(key))
            print(value.rstrip() if value else '(empty)')
    if any(event.get('returncode') != 0 for unused, event in matches):
        raise SystemExit('rpmbuild returned a failure status')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: show-rpmbuild.py TRACE.jsonl')
    main(sys.argv[1])
