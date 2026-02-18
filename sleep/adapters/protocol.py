"""Adapter protocol for vendor sleep data sources.

Both fixture and live adapters implement this interface.
The domain layer depends only on the protocol, never on concrete adapters.
"""

from typing import Protocol, runtime_checkable
from uuid import UUID

from sleep.domain.models import SleepDay


@runtime_checkable
class SleepAdapter(Protocol):
    """Common interface for all vendor sleep data adapters."""

    source_name: str

    def parse(self, raw_response: dict, patient_id: UUID) -> list[SleepDay]:
        """Parse a vendor API response into canonical SleepDay records.

        Args:
            raw_response: The raw vendor API response body.
            patient_id: The patient this data belongs to.

        Returns:
            List of canonical SleepDay objects (may be empty if all records filtered).
        """
        ...
