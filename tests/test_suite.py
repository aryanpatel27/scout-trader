"""pytest entry point: runs the project's self-test suite (offline — no network,
no broker). Each `check` line is a named assertion; a single failure fails CI."""
from trader import selftest


def test_selftest_suite_passes(capsys):
    assert selftest.run() == 0, capsys.readouterr().out
