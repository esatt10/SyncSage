"""The certification bar for a third-party connector package (Step 31.7).

Ship exactly this in your package's test suite: one subclass, one factory.
Passing it is what "pheasant-conformant connector" means — deterministic
unique identities, stable payloads, checkpoint round-trip, full-mode
bypass. Run with plain pytest; `pheasant` is your only test dependency.
"""

from pathlib import Path

from pheasant_connector_example import StaticDirConnector

from pheasant.config.schema import PluginSourceType, SourceConfig
from pheasant.persistence.state_store import StateStore
from pheasant.testing import ConnectorConformance


class TestStaticDirConformance(ConnectorConformance):
    def make_connector(self, tmp_path: Path, state: StateStore) -> StaticDirConnector:
        root = tmp_path / "content"
        root.mkdir(exist_ok=True)
        (root / "sample.txt").write_text("hello from the example connector")
        source = SourceConfig(
            name="example", type=PluginSourceType("staticdir"), path=root, include=[]
        )
        return StaticDirConnector(source, state)
