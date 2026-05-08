"""
Cross-layer data carriers.

Plain dataclasses that flow between routers, services, the pipeline
orchestrator, adapters, and repositories. No behavior beyond
construction — these are the typed shapes that name the boundary
between layers.

Import from the specific submodule rather than this package — e.g.
``from ingestion_api.contracts.ingestion import IngestionInput``.
"""
