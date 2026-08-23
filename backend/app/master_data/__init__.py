"""Master-data pipeline: inspect -> profile -> clean -> validate -> import.

Import the concrete modules (``app.master_data.inspector``, ``.profiler``,
``.validator``, ``.importer``, ``.schema``) directly. This package deliberately
imports none of them: ``importer`` depends on ``utils.cleaning``, which depends
on ``master_data.schema``, so eager re-exports here would form an import cycle.
"""
