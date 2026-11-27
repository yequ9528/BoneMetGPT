"""Product-level orchestration contracts for the LatticeGPT local demo."""

from .contracts import (
    BASE_MATERIAL_MODULUS_GPA,
    STIFFNESS_NAMES,
    target_from_database_record,
    target_from_physical_9d,
)
from .service import ProductWorkflowService

__all__ = [
    "BASE_MATERIAL_MODULUS_GPA",
    "STIFFNESS_NAMES",
    "target_from_database_record",
    "target_from_physical_9d",
    "ProductWorkflowService",
]
