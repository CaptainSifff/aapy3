"""Real JSON signature persistence for the integration CLI."""
from __future__ import print_function

import json
import os
import time

from aap_semantics import PersistenceBackend


class DiskPersistence(PersistenceBackend):
    def __init__(self, recipe_dir, record=None):
        self._record = record if record is not None else (lambda kind, **fields: None)
        self.path = os.path.join(recipe_dir, 'AAPDIR', 'signatures.json')
        self.targets = {}
        existed = os.path.isfile(self.path)
        size = os.path.getsize(self.path) if existed else 0
        if existed:
            with open(self.path) as stream:
                data = json.load(stream)
            self.targets = data.get('targets', {})
        self._record('persistence_load', path=self.path,
                     existed=existed, bytes=size,
                     target_count=len(self.targets))

    def signature(self, target, source, check):
        for item in self.targets.get(target, {}).get('values', ()):
            if item['source'] == source and item['check'] == check:
                self._record('signature_lookup', target=target, source=source,
                             check=check, found=True, value=item['value'])
                return item['value']
        self._record('signature_lookup', target=target, source=source,
                     check=check, found=False, value='')
        return ''

    def marker_exists(self, path):
        return os.path.exists(path)

    def timestamp(self):
        return str(time.time())

    def flush(self, records):
        for record in records:
            if record.values:
                values = []
                for key, value in sorted(record.values.items()):
                    values.append({'source': key[0], 'check': key[1],
                                   'value': value})
                self.targets[record.target] = {'values': values,
                                               'timestamp': record.timestamp}
            else:
                self.targets.pop(record.target, None)
        parent = os.path.dirname(self.path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        temporary = self.path + '.new'
        with open(temporary, 'w') as stream:
            json.dump({'targets': self.targets}, stream, indent=2,
                      sort_keys=True)
            stream.write('\n')
        os.rename(temporary, self.path)
        self._record('persistence_flush', path=self.path,
                     bytes=os.path.getsize(self.path),
                     record_count=len(records), target_count=len(self.targets))

