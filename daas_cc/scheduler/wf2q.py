#
#                © 2026 Nokia
#                Licensed under the BSD 3-Clause Clear License
#                SPDX-License-Identifier: BSD-3-Clause-Clear
#
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

@dataclass
class FlowQueue:
    id: str
    rate_Mbps: int
    T: float = 0.0
    S: float = 0.0
    F: float = 0.0

@dataclass
class NodeScheduler:
    id: str
    children: Dict[str, FlowQueue] = field(default_factory=dict)
    V: float = 0.0
    T: float = 0.0

    def add_child(self, fq: FlowQueue):
        self.children[fq.id] = fq

    def reset(self):
        self.V = 0.0
        for fq in self.children.values():
            fq.S = 0.0
            fq.F = fq.T

    def _eligible(self, fq: FlowQueue) -> bool:
        return fq.S <= self.V + 1e-12

    def _update_virtual_time(self):
        self.V += self.T

    def _update_child_SF(self, fq: FlowQueue):
        fq.S = fq.F
        fq.F = fq.S + fq.T

    def schedule_one_slot(self) -> Optional[str]:
        elig = [fq for fq in self.children.values() if self._eligible(fq) and fq.rate_Mbps > 0]
        if not elig:
            # No child found with eligible virtual start time: look for minimum finish time
            # (a service must always be assigned)
            next_F = min((fq.F for fq in self.children.values() if fq.rate_Mbps > 0), default=None)
            if next_F is None:
                next_F = min((fq.F for fq in self.children.values()), default=None)
            elig = [fq for fq in self.children.values() if fq.F == next_F]
        if not elig:
            print("Error: service candidate not found in {}".format(self.id))
            self._update_virtual_time()
            return None
        chosen = min(elig, key=lambda q: q.F)
        self._update_child_SF(chosen)
        self._update_virtual_time()
        return chosen.id
