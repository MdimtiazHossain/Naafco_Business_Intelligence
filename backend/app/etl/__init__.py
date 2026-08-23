"""Phase 2 ETL: transaction data from any source into the fact layer.

    source -> staging -> validation -> master mapping -> facts -> reporting views

Public entry points:

* :func:`run_import` — the full pipeline for one file or reader
* :class:`MasterDataIndex` — Phase 1 dimensions loaded for validation
* :mod:`errors` — the rejection catalogue
* :mod:`datasets` — per-data-type field mapping and business keys
"""

from .calendar import FinancialYearConfig, populate_dim_date, to_date_id
from .datasets import DATA_TYPES, DATASETS, DatasetSpec, get_dataset
from .mapping import MasterDataIndex, ensure_master_source_status
from .pipeline import (
    LOAD_MODE_INCREMENTAL,
    LOAD_MODE_INITIAL,
    LOAD_MODE_REPROCESS,
    EtlPipeline,
    ImportResult,
    run_import,
)
from .quality import batch_quality, quality_overview, rejected_records
from .readers import CsvSourceReader, ExcelSourceReader, RecordsSourceReader, reader_for_file

__all__ = [
    "DATASETS",
    "DATA_TYPES",
    "DatasetSpec",
    "get_dataset",
    "FinancialYearConfig",
    "populate_dim_date",
    "to_date_id",
    "MasterDataIndex",
    "ensure_master_source_status",
    "EtlPipeline",
    "ImportResult",
    "run_import",
    "LOAD_MODE_INITIAL",
    "LOAD_MODE_INCREMENTAL",
    "LOAD_MODE_REPROCESS",
    "batch_quality",
    "quality_overview",
    "rejected_records",
    "ExcelSourceReader",
    "CsvSourceReader",
    "RecordsSourceReader",
    "reader_for_file",
]
