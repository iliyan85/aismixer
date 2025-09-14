import os
from typing import Dict, Any, Optional
from core.utils.ttlmap import TTLMap

S_CACHE_TTL_S = float(os.getenv("AISMIXER_S_TTL_S", "900"))   # 15 мин
S_CACHE_MAX = int(os.getenv("AISMIXER_S_MAX", "200000"))
SWEEP_EVERY_S = float(os.getenv("AISMIXER_SWEEP_EVERY_S", "1.0"))
OPS_PER_SWEEP = int(os.getenv("AISMIXER_OPS_PER_SWEEP", "2048"))

per_s_state: Dict[str, Dict[str, Any]] = {}


def _on_s_evict(skey: str):
    per_s_state.pop(skey, None)


s_cache = TTLMap(
    ttl_seconds=S_CACHE_TTL_S,
    max_entries=S_CACHE_MAX,
    on_evict=_on_s_evict,
    sweep_every_seconds=SWEEP_EVERY_S,
    ops_per_sweep=OPS_PER_SWEEP,
)


def touch_s(s: Optional[str]) -> None:
    if s:
        s_cache.touch(s)
        if s not in per_s_state:
            per_s_state[s] = {}
