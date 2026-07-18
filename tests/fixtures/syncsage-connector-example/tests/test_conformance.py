"""The certification bar for a third-party connector package (Step 31.7).

Ship exactly this in your package's test suite: one subclass, one factory.
Passing it is what "SyncSage-conformant connector" means — deterministic
unique identities, stable payloads, checkpoint round-trip, full-mode
bypass. Run with plain pytest; `syncsage` is your only test dependency.
"""

from pathlib import Path

from syncsage.config.schema import PluginSourceType, SourceConfig
from syncsage.persistence.state_store import StateStore
from syncsage.testing import ConnectorConformance

from syncsage_connector_example import StaticDirConnector


class TestStaticDirConformance(ConnectorConformance):
    def make_connector(self, tmp_path: Path, state: StateStore) -> StaticDirConnector:
        root = tmp_path / "content"
        root.mkdir(exist_ok=True)
        (root / "sample.txt").write_text("hello from the example connector")
        source = SourceConfig(
            name="example", type=PluginSourceType("staticdir"), path=root, include=[]
        )
        return StaticDirConnector(source, state)
