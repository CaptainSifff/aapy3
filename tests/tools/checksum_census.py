"""Bound checksum usage using generated reports only; never open recipes.

Usage: python3 tests/tools/checksum_census.py
The deliberately narrow structural recognizer reports every nonmatching row.
"""
import collections
import csv
import io
import json
import os
import re


def census(directory):
    with io.open(os.path.join(directory, 'summary.json'), encoding='utf-8') as stream:
        generic = json.load(stream)['per_command']['checksum']
    with io.open(os.path.join(directory, 'command-forms.tsv'), encoding='utf-8', newline='') as stream:
        forms = [r for r in csv.DictReader(stream, delimiter='\t') if r['command'] == 'checksum']
    with io.open(os.path.join(directory, 'command-occurrences.tsv'), encoding='utf-8', newline='') as stream:
        rows = [r for r in csv.DictReader(stream, delimiter='\t') if r['command'] == 'checksum']
    pattern = re.compile(r'^\$(DISTDIR|PATCHDISTDIR)/([^\s{}\'"`]+)\s+\{md5\s*=\s*([0-9a-f]+)\}$')
    variables = collections.Counter()
    lengths = collections.Counter()
    paths = collections.Counter()
    per_recipe_paths = collections.Counter()
    exceptions = []
    unusual_digests = []
    for row in rows:
        match = pattern.match(row['raw_arguments'])
        if match is None:
            exceptions.append({'file': row['file'], 'line': int(row['line']),
                               'arguments': row['raw_arguments']})
            continue
        variable, filename, digest = match.groups()
        variables[variable] += 1
        lengths[str(len(digest))] += 1
        path = '$' + variable + '/' + filename
        paths[path] += 1
        per_recipe_paths[(row['file'], path)] += 1
        if len(digest) != 32:
            unusual_digests.append({'file': row['file'], 'line': int(row['line']),
                                   'digest_length': len(digest)})
    matched = len(rows) - len(exceptions)
    return {
        'inputs': ['command-forms/summary.json', 'command-forms/command-forms.tsv',
                   'command-forms/command-occurrences.tsv'],
        'occurrences': len(rows),
        'generic_occurrences': generic['occurrences'],
        'generic_distinct_forms': generic['distinct_forms'],
        'form_rows': len(forms),
        'form_occurrences': sum(int(r['count']) for r in forms),
        'matched_single_path_then_one_md5_attribute': matched,
        'structural_grammar': '$DISTDIR/FILENAME {md5 = LOWERCASE_HEX} (or $PATCHDISTDIR)',
        'file_item_count_distribution_for_matched_rows': {'1': matched},
        'digest_count_distribution_for_matched_rows': {'1': matched},
        'digest_algorithms_for_matched_rows': {'md5': matched},
        'digest_lengths': dict(lengths),
        'non_32_digit_digest_locations': unusual_digests,
        'variables': dict(variables),
        'whitespace_token_counts': dict(collections.Counter(str(len(r['raw_arguments'].split())) for r in rows)),
        'physical_lines': dict(collections.Counter(r['physical_lines'] for r in rows)),
        'bodies': dict(collections.Counter(r['has_block'] for r in rows)),
        'backticks': dict(collections.Counter(r['has_backtick'] for r in rows)),
        'rows_with_quotes': sum(any(c in r['raw_arguments'] for c in "'\"") for r in rows),
        'distinct_unexpanded_paths': len(paths),
        'repeated_unexpanded_paths': sum(n > 1 for n in paths.values()),
        'repeat_occurrences_beyond_first': sum(n - 1 for n in paths.values()),
        'within_recipe_repeated_paths': sum(n > 1 for n in per_recipe_paths.values()),
        'exceptions': exceptions,
        'notes': [
            'Counts are lexical usage evidence, not verification of archive digests.',
            'The generic attribute counters miss spaced {md5 = ...}; raw_arguments are authoritative here.',
            'No recipe or artifact is opened; no semantic claim is made about private file contents.',
            'Matched rows have no missing digest, extra field, multiple algorithm, quote, or filename without a directory variable.'
        ]
    }


if __name__ == '__main__':
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(json.dumps(census(os.path.join(root, 'tests', 'fixtures', 'command-forms')), indent=2, sort_keys=True))
