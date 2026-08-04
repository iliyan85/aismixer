import os
from typing import Any

from core.utils.ttlmap import TTLMap

S_CACHE_TTL_S = float(os.getenv("AISMIXER_S_TTL_S", "900"))   # 15 мин
S_CACHE_MAX = int(os.getenv("AISMIXER_S_MAX", "200000"))
SWEEP_EVERY_S = float(os.getenv("AISMIXER_SWEEP_EVERY_S", "1.0"))
OPS_PER_SWEEP = int(os.getenv("AISMIXER_OPS_PER_SWEEP", "2048"))


class SourceState:
    """Bounded source activity and associated state for one processor."""

    __slots__ = ("_s_cache", "_per_s_state")

    def __init__(
        self,
        *,
        ttl_seconds: float = S_CACHE_TTL_S,
        max_entries: int = S_CACHE_MAX,
        sweep_every_seconds: float = SWEEP_EVERY_S,
        ops_per_sweep: int = OPS_PER_SWEEP,
    ) -> None:
        self._per_s_state: dict[str, dict[str, Any]] = {}
        self._s_cache = TTLMap(
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            on_evict=self._on_s_evict,
            sweep_every_seconds=sweep_every_seconds,
            ops_per_sweep=ops_per_sweep,
        )

    def touch_s(self, s_value: str | None) -> None:
        if s_value:
            self._s_cache.touch(s_value)
            if s_value not in self._per_s_state:
                self._per_s_state[s_value] = {}

    def _on_s_evict(self, s_key: str) -> None:
        self._per_s_state.pop(s_key, None)
