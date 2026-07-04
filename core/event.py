from dataclasses import dataclass
from typing import Optional, TypeAlias

IngressKind: TypeAlias = str


@dataclass(slots=True)
class IngressEvent:
    kind: IngressKind
    source_id: str
    alias_for_s: Optional[str]   # може да е None (за да уважим s от входа)
    remote_ip: Optional[str]     # за fallback IP->s
    assembler_key: str           # стабилен ключ за сглобяване (никога None)
    raw_line: str                # оригиналният ред (може да съдържа TAG)
