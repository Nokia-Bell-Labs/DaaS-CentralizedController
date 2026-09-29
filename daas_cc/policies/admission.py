#
#                © 2026 Nokia
#                Licensed under the BSD 3-Clause Clear License
#                SPDX-License-Identifier: BSD-3-Clause-Clear
#
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional
from ..domain.models import Topology, Path, TSpecR

@dataclass
class AdmissionResult:
    accepted: bool
    reason: Optional[str]
    selected_path: Optional[Path]
    sr_Mbps: Optional[int]

class AdmissionControl:
    def __init__(self, topo: Topology, slot_duration_s: float, mode: str = 'exact',
                 conservative_cac_max_path_hops:int=8, conservative_cac_max_link_latency_s: float=10e-6,
                 bandwidth_overhead: float = 0.0):
        self.topo = topo
        self.T = slot_duration_s
        self.mode = mode
        self.cons_max_hops = conservative_cac_max_path_hops
        self.cons_max_link_latency_s = conservative_cac_max_link_latency_s
        self.bandwidth_overhead = bandwidth_overhead

    def _path_D_net_bound(self, path: Path, in_links_cover: Dict[str, int]) -> float:
        total = 0.0
        for l in path.links:
            # Accumulate the queuing and propagation latency contributions of all links in the path
            n_e = in_links_cover.get(l, 0)
            d_e = self.topo.links[l].prop_latency_s
            total += 2.0*self.T*n_e + d_e
        return total

    def _domain_upper_bound_D_net(self, max_path_hops:int, n_ingress:int, d_max:float) -> float:
        # The ingress link only contributes the propagation latency
        return (max_path_hops - 1) * (2.0 * self.T * n_ingress + d_max) + d_max

    def required_rate_for_latency(self, B_bytes:int, C_Mbps:int, D_target_s:float, D_net_s:float) -> Optional[float]:

        # DEBUG
        # print("rrfl: B_bytes {} C_Mbps {} D_target {} D_net {}".format(B_bytes, C_Mbps, D_target_s, D_net_s))
        # END DEBUG

        numerator_bits = 8.0*B_bytes + 2.0*self.T*C_Mbps*1e06  # B = 8*B_Ref_bytes per v0.05
        denom = D_target_s - D_net_s
        if denom <= 0:
            return None
        R_Mbps = numerator_bits // (denom * 1e06)
        return R_Mbps

    def reserved_rate_Mbps(self, R_min_Mbps: int, R_req_Mbps: float) -> int:
        base_rate_Mbps = max(float(R_min_Mbps), float(R_req_Mbps))
        if self.bandwidth_overhead > 0:
            return int((100 + self.bandwidth_overhead) * base_rate_Mbps // 100)
        return int(base_rate_Mbps)

    def decide(self, tspec: TSpecR, candidate_paths: List[Path],
               link_residual_Mbps: Dict[str, int], ingress_links_cover: Dict[str, int],
               marginal_cost_fn=None):
        R_min_Mbps = tspec.R_Min_Mbps   # Minimum Ethernet throughput accepted
        B_bytes = tspec.B_Ref_bytes     # Leaky-bucket size for shaping latency
        D_target_s = tspec.D_Max_ns / 1e9  # Maximum end-to-end latency accepted

        # Maximum number of ingress links per interior link based on topology
        n_ingress_total = len(self.topo.ingress_links)

        for path in candidate_paths:
            # Compute first the network traversal latency of the path under consideration
            if self.mode == 'exact':
                # Exact CAC version uses actual fan-in into the links of the path
                D_net = self._path_D_net_bound(path, ingress_links_cover)
            else:
                # Conservative CAC version uses upper bound on link fan-in
                D_net = self._domain_upper_bound_D_net(max_path_hops=self.cons_max_hops, n_ingress=n_ingress_total, d_max=self.cons_max_link_latency_s)

            # DEBUG
            # print("Network traversal latency of path {}: {}".format(path.id, D_net))
            # END DEBUG

            # Capacity of the ingress link for the path under consideration
            C_Mbps = self.topo.links[path.links[0]].capacity_Mbps

            # Shaping rate needed to satisfy the latency requirement of the flow
            R_req_Mbps = self.required_rate_for_latency(B_bytes=B_bytes, C_Mbps=C_Mbps, D_target_s=D_target_s, D_net_s=D_net)
            if R_req_Mbps is None:
                continue
            SR_Mbps = self.reserved_rate_Mbps(R_min_Mbps, R_req_Mbps)

            # Effective rate to check against residuals: marginal bundle cost if a
            # bundle-aware function is provided; otherwise the per-flow sr_Mbps.
            check_Mbps = marginal_cost_fn(path, SR_Mbps) if marginal_cost_fn is not None else SR_Mbps

            # DEBUG
            # print("decide: R_min_Mbps {}  R_req_Mbps {}  SR_Mbps {} Mb/s".format(R_min_Mbps, R_req_Mbps, SR_Mbps))
            # END DEBUG

            # Check on bandwidth availability in path links
            feasible = True
            for l in path.links:
                if link_residual_Mbps.get(l, 0) < check_Mbps:
                    feasible = False
                    # This path is not feasible: stop checking its links
                    break
            if not feasible:
                # Try the next path
                continue

            # The first feasible path is taken
            # DEBUG
            # ("path {} is feasible for flow {}".format(path.id, tspec))
            # END DEBUG
            return dict(accepted=True, reason=None, selected_path=path, sr_Mbps=SR_Mbps)

        # DEBUG
        # print("No path found for flow {}".format(tspec))
        # END DEBUG

        return dict(accepted=False, reason="No feasible path found under residual capacity / latency target.", selected_path=None, sr_Mbps=None)
