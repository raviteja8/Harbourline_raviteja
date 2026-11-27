from collections.abc import Iterator

import pytest

from sim.testing import SimulatorFixture, running_simulator


@pytest.fixture
def simulator() -> Iterator[SimulatorFixture]:
    with running_simulator() as fixture:
        yield fixture
