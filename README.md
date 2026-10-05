# aapy3

A Python 3 port of A-A-P that understands a limited amount of embedded
Python 2 syntax.

This GitHub repository contains the generated single-file launcher and the
frontend and semantic source modules, tests, explicitly supplied recipe
fixtures, and test helpers needed to run CI without files from the parent
checkout. The workflow is `.github/workflows/tests.yml`.

The launcher is generated from the canonical modules by
`work/tools/build_standalone.py`. After changing canonical modules, regenerate
the checked-in bundle with:

```sh
python3 work/tools/build_standalone.py
```

Run all frontend and semantic tests together from the repository root with:

```sh
PYTHONPATH=work/src:work/tests/semantics:work/docker/recursive-aap \
  python3 -m unittest discover -s work/tests -p 'test*.py' -v
python3 work/docker/recursive-aap/test_cli_stdout.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
```
