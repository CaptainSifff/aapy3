# aapy3

A Python 3 port of A-A-P that understands a limited amount of embedded
Python 2 syntax.

All test code and support files live under `tests/`: the frontend and semantic
tests, runtime source, adapters, tools, and explicit test fixtures. The GitHub
workflow is `.github/workflows/tests.yml`.

The standalone launcher is generated from `tests/src` and `tests/adapters` by
`tests/tools/build_standalone.py`:

```sh
python3 tests/tools/build_standalone.py
```

GitHub Actions also builds and installs the fixture Nano 7.1 RPM in disposable
SLES 15 SP6, SLES 15 SP7, and openSUSE Leap 16.0 containers. These jobs run the
real configure, compile, staged install, and `rpmbuild` steps through the A-A-P
CLI, then inspect and smoke-test the produced RPM. Logs, traces, and packages
are uploaded as workflow artifacts.

Run the suites from the repository root with:

```sh
PYTHONPATH=tests/src:tests/semantics:tests/adapters:tests/tools \
  python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/adapters/test_cli_stdout.py
```
