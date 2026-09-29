#
#                © 2026 Nokia
#                Licensed under the BSD 3-Clause Clear License
#                SPDX-License-Identifier: BSD-3-Clause-Clear
#
from __future__ import annotations
from dataclasses import dataclass
from functools import total_ordering
from typing import Dict, List, Tuple, Optional
import bisect
from ..domain.models import Topology, Path
from ..utils.graph import k_shortest_paths_by_hops, k_shortest_paths_by_latency, path_max_loaded_link_residual_fraction

@dataclass
class RoutingMatrix:
    paths: Dict[Tuple[str,str], List[Path]]

class RoutingPolicy:
    def __init__(self, max_path_num: int = 8,
                 max_hop_num: int = 4,
                 max_latency_num_us: float = 100000.0):
        self.max_path_num = max_path_num
        self.max_hop_num = max_hop_num
        self.max_latency_num_us = max_latency_num_us

    def build_routing_matrix(self, topo: Topology, residual_Mbps: Dict[str, int], k:int=8) -> RoutingMatrix:
        raise NotImplementedError

class ShortestHopsPolicy(RoutingPolicy):
    def build_routing_matrix(self, topo: Topology, residual_Mbps: Dict[str, int], k:int=8) -> RoutingMatrix:
        mapping: Dict[Tuple[str,str], List[Path]] = {}
        endpoints = [n for n,d in topo.nodes.items() if d.kind == 'endpoint']

        for s in endpoints:
            for t in endpoints:
                if s == t: continue
                cands = k_shortest_paths_by_hops(topo, s, t, k=k)
                mapping[(s,t)] = sorted(cands, key=lambda p: (p.hop_count, p.total_prop_latency_s))

                # DEBUG
                #print("mapping({}, {} before trimming: {}".format(s,t,mapping[(s,t)]))
                # END DEBUG

                mapping[(s,t)] = [d for d in mapping[(s,t)] if
                                  (mapping[(s,t)].index(d) < self.max_path_num and
                                   d.hop_count <= self.max_hop_num and
                                   d.total_prop_latency_s * 1000000 <= self.max_latency_num_us)]
                # DEBUG
                #print("mapping({}, {} after trimming: {}".format(s,t,mapping[(s,t)]))
                # END DEBUG

        return RoutingMatrix(paths=mapping)

class ShortestLatencyPolicy(RoutingPolicy):
    def build_routing_matrix(self, topo: Topology, residual_Mbps: Dict[str, int], k:int=8) -> RoutingMatrix:
        mapping: Dict[Tuple[str,str], List[Path]] = {}
        endpoints = [n for n,d in topo.nodes.items() if d.kind == 'endpoint']
        for s in endpoints:
            for t in endpoints:
                if s == t: continue
                cands = k_shortest_paths_by_latency(topo, s, t, k=k)
                mapping[(s,t)] = sorted(cands, key=lambda p: (p.total_prop_latency_s, p.hop_count))

                # DEBUG
                #print("mapping({}, {} before trimming: {}".format(s,t,mapping[(s,t)]))
                # END DEBUG

                mapping[(s,t)] = [d for d in mapping[(s,t)] if
                                  (mapping[(s,t)].index(d) < self.max_path_num and
                                   d.hop_count <= self.max_hop_num and
                                   d.total_prop_latency_s * 1000000 <= self.max_latency_num_us)]
                # DEBUG
                #print("mapping({}, {} after trimming: {}".format(s,t,mapping[(s,t)]))
                # END DEBUG

        return RoutingMatrix(paths=mapping)

class HighestSpareMinLinkPolicy(RoutingPolicy):
    def build_routing_matrix(self, topo: Topology, residual_Mbps: Dict[str, int], k:int=8) -> RoutingMatrix:
        """
        Iteratively select the best path based on largest residual bandwidth of bottleneck links,
        excluding links that are common to all remaining candidate paths.

        Algorithm:
        1. Start with all available paths for (source, destination)
        2. Remove links shared by ALL paths from consideration
        3. For each path, find the link with lowest residual bandwidth (bottleneck)
        4. Select the link(s) with largest residual bandwidth among the bottlenecks
        5. If multiple paths share this link, iterate (go to step 2 with those paths)
        6. If only one path has this link, that path wins
        """
        mapping: Dict[Tuple[str,str], List[Path]] = {}
        endpoints = [n for n,d in topo.nodes.items() if d.kind == 'endpoint']

        for s in endpoints:
            for t in endpoints:
                if s == t:
                    continue

                mapping[(s,t)] = self.order_paths_for_pair(topo, residual_Mbps, s, t, k=k)

        return RoutingMatrix(paths=mapping)

    def order_paths_for_pair(
        self,
        topo: Topology,
        residual_Mbps: Dict[str, int],
        src: str,
        dst: str,
        k: int = 8,
    ) -> List[Path]:
        """
        Return candidate paths for one (src, dst) pair ordered by the iterative
        max-min residual-bandwidth rule used by this policy.
        """
        # Step 1: Get all candidate paths
        candidates = k_shortest_paths_by_hops(topo, src, dst, k=4 * k)

        if not candidates:
            return []

        # Apply hop and latency constraints upfront
        candidates = [
            p for p in candidates
            if p.hop_count <= self.max_hop_num
            and p.total_prop_latency_s * 1000000 <= self.max_latency_num_us
        ]

        if not candidates:
            return []

        # Iteratively select paths using the policy algorithm
        selected_paths: List[Path] = []
        remaining = candidates[:]

        while remaining and len(selected_paths) < self.max_path_num:
            winner = self._select_best_path(remaining, residual_Mbps, topo)
            if winner:
                selected_paths.append(winner)
                remaining = [p for p in remaining if p.id != winner.id]
            else:
                break

        return selected_paths

    def _select_best_path(self, paths: List[Path], residual_Mbps: Dict[str, int], topo: Topology) -> Optional[Path]:
        """
        Select the best path from candidates using iterative bottleneck link analysis.

        Returns:
            The winning path, or None if all paths are eliminated
        """
        if not paths:
            return None

        if len(paths) == 1:
            return paths[0]

        current_candidates = paths[:]

        # Iterative selection process
        max_iterations = 100  # Safety limit
        for iteration in range(max_iterations):
            if len(current_candidates) == 1:
                return current_candidates[0]

            if not current_candidates:
                return None

            # Step 2: Find links shared by ALL current candidates
            # Start with links from first path
            common_links = set(self._get_interior_links(current_candidates[0], topo))
            for path in current_candidates[1:]:
                interior = set(self._get_interior_links(path, topo))
                common_links = common_links.intersection(interior)

            # Step 3: For each path, find bottleneck link (excluding common links)
            path_bottlenecks = {}  # path -> (bottleneck_link, residual_bw)

            for path in current_candidates:
                interior = self._get_interior_links(path, topo)
                # Exclude common links
                candidate_links = [l for l in interior if l not in common_links]

                if not candidate_links:
                    # All interior links are common - use all interior links as fallback
                    candidate_links = interior

                if not candidate_links:
                    # Path has no interior links (direct connection)
                    path_bottlenecks[path.id] = (None, float('inf'))
                    continue

                # Find link with minimum residual bandwidth (bottleneck)
                min_residual = float('inf')
                bottleneck_link = None
                for link in candidate_links:
                    residual = residual_Mbps.get(link, 0)
                    if residual < min_residual:
                        min_residual = residual
                        bottleneck_link = link

                path_bottlenecks[path.id] = (bottleneck_link, min_residual)

            # Step 4: Find the largest residual bandwidth among bottlenecks
            max_bottleneck_bw = max(bw for _, bw in path_bottlenecks.values())

            # Find all paths with this maximum bottleneck bandwidth
            best_paths = [p for p in current_candidates
                         if path_bottlenecks[p.id][1] == max_bottleneck_bw]

            if not best_paths:
                # Should not happen, but safety check
                return current_candidates[0]

            # Step 5: Check if the best bottleneck link is shared by multiple paths
            best_bottleneck_links = set()
            for path in best_paths:
                link, _ = path_bottlenecks[path.id]
                if link is not None:
                    best_bottleneck_links.add(link)

            # If only one path has the best bottleneck, it wins
            if len(best_paths) == 1:
                return best_paths[0]

            # If multiple paths share the same best bottleneck link, iterate with them
            # This implements step 5 of the algorithm
            current_candidates = best_paths

        # Fallback: if we hit iteration limit, return first candidate
        return current_candidates[0] if current_candidates else None

    def _get_interior_links(self, path: Path, topo: Topology) -> List[str]:
        """
        Get interior links of a path (excluding ingress and egress links).

        Args:
            path: The path to analyze
            topo: The topology

        Returns:
            List of interior link IDs
        """
        # Exclude first (ingress) and last (egress) links
        if len(path.links) <= 2:
            return []

        return path.links[1:-1]
# The criterion for limiting the number of paths per endpoint pair should be applied right away
# before returning the routing matrix. Look at the way the admission control policy uses its
# parameters for the exact and conservative options.
# The number of paths is controlled by k; the main challenge is retrieving the limiting parameters
# from the configuration class
