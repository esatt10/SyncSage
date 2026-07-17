"""First-party connector plugins (Phase 31).

Each connector here rides the Step-31.1 SDK exactly as a third-party
package would: declared under the ``syncsage.connectors`` entry-point
group in ``pyproject.toml``, resolved by ``sources[].type`` name at
dispatch, and held to ``syncsage.testing.ConnectorConformance``.
"""
