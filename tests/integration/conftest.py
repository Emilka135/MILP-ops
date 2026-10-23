"""Real semantic inference shared by protocol tests, without speech/remote APIs."""
import pytest

@pytest.fixture(scope='session')
def semantic_nlu():
    from clinic.modules import Nlu
    return Nlu('e5')
