from __future__ import annotations

from importlib.metadata import version

import pytest

from topology_lantern import __version__
from topology_lantern.cli import main
from topology_lantern.search import generate_candidates
from topology_lantern.spec import DesignSpec


def test_runtime_distribution_cli_and_report_versions_agree(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert __version__ == version("topology-lantern") == "0.2.0"
    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == f"topology-lantern {__version__}\n"
    result = generate_candidates(
        DesignSpec.from_mapping({"name": "version", "supply_voltage": 1.8}), limit=1
    )
    assert result.as_dict()["tool"] == {"name": "TopologyLantern", "version": __version__}
