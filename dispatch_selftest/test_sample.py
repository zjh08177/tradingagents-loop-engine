"""Fixture suite for run_verify's self-test. NOT auto-collected (see pytest.ini
testpaths). run_verify drives this in a subprocess to prove it parses real
pytest outcomes into ledger verdicts.
"""


def test_sample_pass():
    assert True


def test_sample_fail():
    assert False, "intentional failure — proves run_verify records a real fail"
