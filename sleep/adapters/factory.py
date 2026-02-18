"""Adapter factory: returns fixture or live adapter based on config.

In fixture mode, adapters parse raw payloads passed directly.
In live mode, adapters can also fetch from vendor APIs before parsing.
Both implement the same SleepAdapter protocol (parse method).
"""

from shared.config import settings
from sleep.adapters.protocol import SleepAdapter


def get_adapter(source: str) -> SleepAdapter:
    """Return the appropriate adapter for the given source and adapter_mode.

    - fixture mode: returns fixture adapter (parse-only, no HTTP)
    - live mode: returns live adapter (can fetch + parse)
    """
    if settings.adapter_mode == "live":
        return _get_live_adapter(source)
    return _get_fixture_adapter(source)


def _get_fixture_adapter(source: str) -> SleepAdapter:
    from sleep.adapters.oura_fixture import OuraFixtureAdapter
    from sleep.adapters.withings_fixture import WithingsFixtureAdapter

    adapters: dict[str, SleepAdapter] = {
        "oura": OuraFixtureAdapter(),
        "withings": WithingsFixtureAdapter(),
    }
    adapter = adapters.get(source)
    if adapter is None:
        raise ValueError(f"Unsupported source: {source}. Must be one of: {list(adapters.keys())}")
    return adapter


def _get_live_adapter(source: str) -> SleepAdapter:
    from sleep.adapters.oura_live import OuraLiveAdapter
    from sleep.adapters.withings_live import WithingsLiveAdapter

    adapters: dict[str, SleepAdapter] = {
        "oura": OuraLiveAdapter(),
        "withings": WithingsLiveAdapter(),
    }
    adapter = adapters.get(source)
    if adapter is None:
        raise ValueError(f"Unsupported source: {source}. Must be one of: {list(adapters.keys())}")
    return adapter
