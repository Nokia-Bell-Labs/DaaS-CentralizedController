#
#                © 2026 Nokia
#                Licensed under the BSD 3-Clause Clear License
#                SPDX-License-Identifier: BSD-3-Clause-Clear
#
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import Dict, List, Tuple, Optional

from ..domain.models import Topology, Node, Link, Path, Flow, TSpecR, TSpecC
from ..config import ControllerConfig
from .repository import JsonRepo
from .flow_lists import load_routed_flows, load_unrouted_flows, render_rfl
from .pss_config import IngressProfile, FlowProfile, write_endpoint_configs
from ..policies.routing import RoutingPolicy, ShortestLatencyPolicy, ShortestHopsPolicy, HighestSpareMinLinkPolicy, RoutingMatrix
from ..policies.admission import AdmissionControl
from ..utils.graph import get_all_links_from_iie, parse_path_id_iie
from ..utils.multicast_vlan_mapper import MulticastVLANMapper
from daas_cc.scheduler.pss import IngressRASPlan as _Plan, build_ingress_pss, IngressRASPlan
import asyncio
import os, json, uuid, time
import logging
import contextlib


@dataclass
class DaaSController:
    cfg: ControllerConfig
    repo: JsonRepo = field(init=False)
    topo: Topology = field(init=False)
    residual_Mbps: Dict[str, int] = field(default_factory=dict)
    routing_matrix: RoutingMatrix = field(init=False)
    flows: List[Flow] = field(default_factory=list)
    last_pushed_profiles: Dict[str, 'IngressProfile'] = field(default_factory=dict)  # Last profiles written to disk
    affected_ingresses: set = field(default_factory=set)  # Track ingresses affected by recent flow changes
    runtime_reference_unit_cost: float = field(default=0.0)  # Runtime reference unit cost (can be modified via API)
    _missing_vlan_paths: set = field(default_factory=set)
    multicast_vlan_mapper: Optional[MulticastVLANMapper] = field(default=None, repr=False)

    def __post_init__(self):
        # Initialize runtime_reference_unit_cost from config
        self.runtime_reference_unit_cost = self.cfg.reference_unit_cost
        # Bundle-aware admission control state.
        # key: (ingress_link, path_id, sr_Mbps, l_max_bytes) -> [(df_id, r_min_Mbps), ...]
        # Flows are stored in admission order; packing mirrors _pack_bundles_for_ingress.
        self.bundle_groups: Dict[Tuple, List[Tuple[str, int]]] = {}
        # df_id -> bundle group key for O(1) lookup during removal.
        self.flow_bundle_key: Dict[str, Tuple] = {}
        self.repo = JsonRepo(state_dir=self.cfg.state_dir)
        self.topo = self.repo.load_topology(self.cfg.topology_file)
        self._load_multicast_vlan_map()

        self.residual_Mbps = {lid: int(l.capacity_Mbps * self.cfg.deterministic_capacity_fraction) for lid, l in self.topo.links.items()}  # v0.05 spare capacity cap
        self._recompute_routing_matrix()
        if self.cfg.pre_routed_flows_file and os.path.exists(self.cfg.pre_routed_flows_file):
            self._load_pre_routed(self.cfg.pre_routed_flows_file)
        if self.cfg.pre_unrouted_flows_file and os.path.exists(self.cfg.pre_unrouted_flows_file):
            self._load_and_admit_unrouted(self.cfg.pre_unrouted_flows_file)
        if self.cfg.flow_table_log_file and os.path.exists(self.cfg.flow_table_log_file):
            logging.info(f"Loading flow table log from config: {self.cfg.flow_table_log_file}")
            self._load_pre_routed(self.cfg.flow_table_log_file)

    def _load_multicast_vlan_map(self) -> None:
        """Load the multicast path-to-VLAN file. Unicast VLANs stay in the topology."""
        self.multicast_vlan_mapper = None
        mapping_file = self.cfg.multicast_vlan_mapping_file
        if not mapping_file:
            return
        if not os.path.exists(mapping_file):
            logging.warning("Multicast VLAN mapping file not found: %s", mapping_file)
            return
        mapper = MulticastVLANMapper(mapping_file, topology=self.topo)
        self.multicast_vlan_mapper = mapper
        if mapper.is_loaded():
            stats = mapper.get_statistics()
            logging.info(
                "Multicast VLAN map loaded: %s VLANs from %s",
                stats["total_vlans"], mapping_file,
            )
        else:
            logging.warning("Multicast VLAN map %s contained no entries", mapping_file)

    def _multicast_vlan(self, path_id: str, src_id: str, dst_ids: List[str]) -> Optional[str]:
        """VLAN for a multicast tree.

        The mapping file is consulted first. A topology paths entry is used
        when that file has no match, so a path can still be pinned by hand.
        """
        vlan_id = None
        mapper = self.multicast_vlan_mapper
        if mapper is not None and mapper.is_loaded():
            vlan_id = mapper.get_vlan_for_multicast(
                source=src_id, destinations=dst_ids, iie_path=path_id,
            )
        if not vlan_id:
            vlan_id = self._vlan_id_for_path(path_id)
        if not vlan_id:
            self._note_missing_multicast_vlan(path_id, src_id, dst_ids)
        return vlan_id

    def _routing_policy_from_cfg(self, name: str) -> RoutingPolicy:
        if name == "shortest_hops":
            return ShortestHopsPolicy(max_path_num=self.cfg.routing_max_path_num,
                                      max_hop_num=self.cfg.routing_max_hop_num,
                                      max_latency_num_us=self.cfg.routing_max_latency_num_us)
        if name == "shortest_latency":
            return ShortestLatencyPolicy(max_path_num=self.cfg.routing_max_path_num,
                                         max_hop_num=self.cfg.routing_max_hop_num,
                                         max_latency_num_us=self.cfg.routing_max_latency_num_us)
        if name == "largest_residual_bandwidth":
            return HighestSpareMinLinkPolicy(max_path_num=self.cfg.routing_max_path_num,
                                             max_hop_num=self.cfg.routing_max_hop_num,
                                             max_latency_num_us=self.cfg.routing_max_latency_num_us)
        if name == "largest_residual_bandwidth_minimum_tree":
            return HighestSpareMinLinkPolicy(max_path_num=self.cfg.routing_max_path_num,
                                             max_hop_num=self.cfg.routing_max_hop_num,
                                             max_latency_num_us=self.cfg.routing_max_latency_num_us)
        raise ValueError(f"Unknown routing_policy: {name}")

    def _is_load_aware_routing_policy(self) -> bool:
        return self.cfg.routing_policy in (
            "largest_residual_bandwidth",
            "largest_residual_bandwidth_minimum_tree",
        )

    def _recompute_routing_matrix(self):
        policy = self._routing_policy_from_cfg(self.cfg.routing_policy)

        # DEBUG
        # print("policy configurations: {}".format(self.cfg))
        # END DEBUG
        self.routing_matrix = policy.build_routing_matrix(self.topo, self.residual_Mbps, k=8)

    def _validate_prerouted_flow(self, src_id: str, dst_ids: List[str], path_id: str) -> Tuple[bool, str]:
        """
        Validate consistency between destination endpoints and path links.

        Uses the routing matrix to verify that the declared path can actually reach
        all destination endpoints. This is topology-agnostic.

        Checks:
        1. All destination endpoints are declared in dst_id and have egress links
        2. All interior links exist in topology
        3. The complete path (ingress + interior + egress) forms a valid multicast tree
           that reaches all declared destinations

        Args:
            src_id: Source endpoint ID
            dst_ids: List of destination endpoint IDs
            path_id: Path ID in IIE format (ingress^interior^egress)

        Returns:
            Tuple of (is_valid, error_message)
        """
        from ..utils.graph import parse_path_id_iie
        import networkx as nx

        # Parse the IIE path
        ingress_links, interior_links, egress_links = parse_path_id_iie(path_id)

        if not ingress_links or not egress_links:
            return False, "Path must have at least one ingress and one egress link"

        # Extract destination endpoints from egress links
        # Egress links have format: node_endpoint (e.g., "l02_u01l02")
        reachable_dsts = set()

        for egress_link in egress_links:
            # Verify egress link exists in topology
            if egress_link not in self.topo.links:
                return False, f"Egress link not in topology: {egress_link}"

            # Get destination endpoint from link's destination node
            link = self.topo.links[egress_link]
            dst_node = link.dst

            # Verify destination node is an endpoint
            if dst_node not in self.topo.nodes:
                return False, f"Egress link destination node not found: {dst_node}"

            node = self.topo.nodes[dst_node]
            if node.kind != 'endpoint':
                return False, f"Egress link terminates at non-endpoint node: {dst_node}"

            reachable_dsts.add(dst_node)

        # Check 1: All declared destinations must have egress links
        declared_dsts = set(dst_ids)
        unreachable = declared_dsts - reachable_dsts
        if unreachable:
            return False, f"Destination(s) missing egress links: {', '.join(unreachable)}"

        extra_dsts = reachable_dsts - declared_dsts
        if extra_dsts:
            return False, f"Path has egress to undeclared destination(s): {', '.join(extra_dsts)}"

        # Check 2: Verify all interior links exist in topology
        for interior_link in interior_links:
            if interior_link not in self.topo.links:
                return False, f"Interior link not in topology: {interior_link}"

        # Check 3: Verify path connectivity using topology graph
        # Build a directed graph from the complete set of links (ingress + interior + egress)
        all_path_links = ingress_links + interior_links + egress_links

        # Create subgraph with only the links in this path
        path_graph = nx.DiGraph()

        for link_id in all_path_links:
            if link_id not in self.topo.links:
                return False, f"Link not in topology: {link_id}"

            link = self.topo.links[link_id]
            path_graph.add_edge(link.src, link.dst)

        # Verify ingress link originates from source endpoint
        ingress_link = self.topo.links[ingress_links[0]]
        if ingress_link.src != src_id:
            return False, f"Ingress link does not originate from source {src_id}"

        # Verify all destination endpoints are reachable from source
        unreachable_dsts = []
        for dst_id in dst_ids:
            # Check if there's a path from source to this destination in the path graph
            if not nx.has_path(path_graph, src_id, dst_id):
                unreachable_dsts.append(dst_id)

        if unreachable_dsts:
            return False, (f"Destination(s) not reachable via declared path links: {', '.join(unreachable_dsts)}. "
                          f"The path links do not form a valid multicast tree from {src_id} to all destinations.")

        # Check 4: Verify no dangling links (links that don't contribute to reaching destinations)
        # All links should be part of some path from source to at least one destination
        contributing_links = set()

        for dst_id in dst_ids:
            # Find all simple paths from source to this destination
            try:
                for path in nx.all_simple_paths(path_graph, src_id, dst_id):
                    # Mark all links in this path as contributing
                    for i in range(len(path) - 1):
                        # Find the link ID for this edge
                        src_node = path[i]
                        dst_node = path[i + 1]
                        for link_id in all_path_links:
                            link = self.topo.links[link_id]
                            if link.src == src_node and link.dst == dst_node:
                                contributing_links.add(link_id)
                                break
            except nx.NetworkXNoPath:
                # This shouldn't happen as we already checked connectivity above
                pass

        # Check if any links don't contribute
        non_contributing = set(all_path_links) - contributing_links
        if non_contributing:
            return False, (f"Path contains link(s) that don't contribute to reaching any destination: "
                          f"{', '.join(non_contributing)}")

        return True, ""

    def _vlan_id_for_path(self, path_id: str) -> Optional[str]:
        """Return the topology path-table VLAN for this path, as a hex string.

        Unicast profiles read this table directly. Multicast consults the
        VLAN map first and uses this table only when the map has no entry.
        """
        raw = self.topo.paths.get(path_id)
        if raw is None or raw == "":
            return None
        try:
            if isinstance(raw, str) and raw.lower().startswith("0x"):
                return hex(int(raw, 16))
            return hex(int(raw))
        except (ValueError, TypeError):
            logging.warning("Invalid VLAN id %r for path %s", raw, path_id)
            return None

    def _note_missing_multicast_vlan(self, path_id: str, src_id: str, dst_ids: List[str]) -> None:
        if path_id in self._missing_vlan_paths:
            return
        self._missing_vlan_paths.add(path_id)
        logging.warning(
            "No VLAN mapping for multicast path %s (%s -> %s).",
            path_id, src_id, dst_ids,
        )

    def _load_pre_routed(self, path: str):
        data = load_routed_flows(path, self.topo)
        loaded_count = 0
        rejected_count = 0

        for d in data:
            # Take the flow ID from the file or generate one randomly
            fid = d.get("DF_ID") or str(uuid.uuid4())

            # Convert legacy path_id to IIE format if needed
            path_id = d["path_id"]
            if '^' not in path_id:
                # Legacy format - convert to IIE
                path_id = self._convert_legacy_path_to_iie(path_id)

            # Get destination IDs (always a list in new format)
            dst_ids = d["dst_id"]
            if not isinstance(dst_ids, list):
                dst_ids = [dst_ids]  # Convert legacy string format to list

            # Validate flow consistency
            is_valid, error_msg = self._validate_prerouted_flow(d["src_id"], dst_ids, path_id)
            if not is_valid:
                print(f"WARNING: Rejecting pre-routed flow {fid[:8]}... from {d['src_id']} to {dst_ids}")
                print(f"         Reason: {error_msg}")
                print(f"         Path: {path_id}")
                rejected_count += 1
                continue

            vlan_id = None
            if len(dst_ids) > 1:
                vlan_id = self._multicast_vlan(path_id, d["src_id"], dst_ids)

            flow = Flow(
                df_id=fid,
                src_id=d["src_id"],
                dst_id=dst_ids,
                pcs=d["PCS_R"],
                path_id=path_id,
                sr_Mbps=d["SR_Mbps"],
                l_max_bytes=d["L_Max_bytes"],
                b_ref_bytes=d["B_Ref_bytes"],
                d_max_ns=d["D_Max_ns"],
                unit_cost=float(d.get("unit_cost", 0.0)),
                r_min_Mbps=int(d.get("R_Min_Mbps", 0)),
                vlan_id=vlan_id  # Assign VLAN ID (None for unicast, hex string for multicast)
            )
            self._apply_flow_add(flow)
            loaded_count += 1
            # PSS configuration files are written when /pss/push is called.

        print(f"Pre-routed flows: {loaded_count} loaded, {rejected_count} rejected")
        if rejected_count > 0:
            print(f"WARNING: {rejected_count} flows were rejected due to validation errors")

    def _convert_legacy_path_to_iie(self, legacy_path_id: str) -> str:
        """
        Convert a legacy path ID (any format with hyphens) to IIE format.

        Classifies links as ingress/interior/egress based on topology.
        """
        links = legacy_path_id.split('-')

        ingress_links = []
        interior_links = []
        egress_links = []

        for link in links:
            if link in self.topo.ingress_links:
                ingress_links.append(link)
            elif link in self.topo.egress_links:
                egress_links.append(link)
            else:
                interior_links.append(link)

        # Sort each group alphabetically
        ingress_str = '-'.join(sorted(ingress_links)) if ingress_links else ""
        interior_str = '-'.join(sorted(interior_links)) if interior_links else ""
        egress_str = '-'.join(sorted(egress_links)) if egress_links else ""

        return f"{ingress_str}^{interior_str}^{egress_str}"

    def _derive_log_file_path(self) -> str:
        """Derive the flow table log file path from the topology file name."""
        from pathlib import Path as _Path
        topo_path = _Path(self.cfg.topology_file)
        stem = topo_path.stem  # e.g. "taxonomy-grid-topo"
        if stem.endswith("-topo"):
            stem = stem[:-5]   # -> "taxonomy-grid"
        log_filename = f"routed-{stem}-log.rfl"
        return str(topo_path.parent / log_filename)

    def save_flow_table_log(self, file_name: Optional[str] = None) -> dict:
        """Write the current flow table as a .rfl routed flow list.

        If file_name is provided, it overrides the default derived path.
        Relative file names are resolved relative to the topology file directory.
        The written file always uses the .rfl suffix.
        """
        from pathlib import Path as _Path
        if file_name is not None:
            p = _Path(file_name)
            if not p.is_absolute():
                p = _Path(self.cfg.topology_file).parent / p
        else:
            p = _Path(self._derive_log_file_path())
        if p.suffix.lower() != ".rfl":
            p = p.with_suffix(".rfl")
        log_path = str(p)
        flow_list = [
            {
                "src_id": flow.src_id,
                "dst_id": flow.dst_id,
                "path_id": flow.path_id,
                "SR_Mbps": flow.sr_Mbps,
                "L_Max_bytes": flow.l_max_bytes,
                "D_Max_ns": flow.d_max_ns,
            }
            for flow in self.flows
        ]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_rfl(flow_list), encoding="utf-8")
        logging.info(f"Flow table log saved: {log_path} ({len(flow_list)} flows)")
        return {
            "status": "success",
            "file_path": log_path,
            "flow_count": len(flow_list),
        }

    def load_flow_table_log(self, path: str) -> dict:
        """Load a .rfl routed flow list into the current flow table.

        Respects the allow_load_log_with_existing_flows configuration option.
        If the path is relative, it is resolved relative to the topology file directory.
        """
        from pathlib import Path as _Path
        resolved = _Path(path)
        if not resolved.is_absolute():
            resolved = _Path(self.cfg.topology_file).parent / resolved
        path = str(resolved)

        if resolved.suffix.lower() != ".rfl":
            return {"accepted": False, "reason": f"Expected a .rfl file: {path}"}

        if not os.path.exists(path):
            return {"accepted": False, "reason": f"File not found: {path}"}

        current_flow_count = len(self.flows)
        if current_flow_count > 0 and not self.cfg.allow_load_log_with_existing_flows:
            return {
                "accepted": False,
                "reason": (
                    f"Cannot load flow table log: {current_flow_count} flow(s) already present. "
                    "Set 'allow_load_log_with_existing_flows': true in the configuration to allow "
                    "loading when flows already exist."
                ),
            }

        total_in_file = len(load_routed_flows(path, self.topo))

        count_before = len(self.flows)
        self._load_pre_routed(path)
        count_after = len(self.flows)
        loaded = count_after - count_before
        rejected = total_in_file - loaded

        logging.info(f"Flow table log loaded from {path}: {loaded} loaded, {rejected} rejected")
        return {
            "accepted": True,
            "file_path": path,
            "flows_in_file": total_in_file,
            "flows_loaded": loaded,
            "flows_rejected": rejected,
            "total_flows": count_after,
        }

    def _load_and_admit_unrouted(self, path: str):
        data = load_unrouted_flows(path, self.topo)
        total = len(data)
        print(f"[INFO] _load_and_admit_unrouted: processing {total} flows from {path}")
        t_start = time.perf_counter()
        progress_interval = self.cfg.flow_routing_progress_interval_s
        t_last_progress = t_start
        accepted_count = 0
        rejected_count = 0
        for i, d in enumerate(data):
            tspec = TSpecR(**d)
            # DEBUG
            # print("tspec {}".format(tspec))
            # END DEBUG

            result = self.add_flow(tspec)
            if result.get("accepted"):
                accepted_count += 1
            else:
                rejected_count += 1

            if progress_interval > 0:
                now = time.perf_counter()
                if now - t_last_progress >= progress_interval:
                    elapsed = now - t_start
                    processed_count = i + 1
                    rate = processed_count / elapsed if elapsed > 0 else 0.0
                    eta = (total - processed_count) / rate if rate > 0 else float('inf')
                    print(f"[INFO] _load_and_admit_unrouted: {processed_count}/{total} flows processed "
                          f"({accepted_count} accepted, {rejected_count} rejected, "
                          f"{elapsed:.1f}s elapsed, {rate:.1f} flows processed/s, "
                          f"ETA {eta:.0f}s)")
                    t_last_progress = now

        elapsed = time.perf_counter() - t_start
        print(f"[INFO] _load_and_admit_unrouted: finished processing {total} flows in {elapsed:.3f}s "
              f"({elapsed/total*1000:.2f} ms/flow) — {accepted_count} accepted, {rejected_count} rejected")

    def _group_total_bw(self, key: Tuple, flow_list: List[Tuple[str, int]]) -> int:
        """Total effective scheduled bandwidth for all bundles in a group.

        Mirrors the packing logic of _pack_bundles_for_ingress: flows are packed
        into bundles of at most max_per_bundle flows (FFD, same l_max_bytes so FIFO),
        and each bundle's rate is max(sr_Mbps, overhead-adjusted sum of r_min values).
        """
        ingress_link, path_id, sr_Mbps, l_max_bytes = key
        overhead = self.cfg.bandwidth_overhead
        capacity = self.topo.links[ingress_link].capacity_Mbps
        bytes_per_slot = int(1e6 * capacity * self.cfg.slot_duration_s) // 8
        max_per_bundle = max(1, bytes_per_slot // l_max_bytes) if l_max_bytes > 0 else 1
        r_mins = [r for _, r in flow_list]
        total = 0
        for i in range(0, len(r_mins), max_per_bundle):
            chunk = r_mins[i:i + max_per_bundle]
            total += max(sr_Mbps, int((100.0 + overhead) * sum(chunk) / 100.0))
        return total

    def _bundle_marginal_bw(self, ingress_link: str, path_id: str,
                             sr_Mbps: int, l_max_bytes: int, r_min_Mbps: int) -> int:
        """Marginal scheduled bandwidth added when one new flow joins a bundle group."""
        key = (ingress_link, path_id, sr_Mbps, l_max_bytes)
        current = self.bundle_groups.get(key, [])
        before = self._group_total_bw(key, current)
        after = self._group_total_bw(key, current + [("_new_", r_min_Mbps)])
        return after - before

    def _bundle_removal_bw(self, df_id: str) -> Tuple[int, List[str]]:
        """Marginal bandwidth freed on every path link when a tracked flow is removed.

        Returns (bw_to_restore, path_links).  Falls back to sr_Mbps for flows that
        pre-date bundle tracking (e.g., loaded from an old log file).
        """
        key = self.flow_bundle_key.get(df_id)
        if key is None:
            flow = next((f for f in self.flows if f.df_id == df_id), None)
            if flow:
                return flow.sr_Mbps, get_all_links_from_iie(flow.path_id)
            return 0, []
        current = self.bundle_groups.get(key, [])
        before = self._group_total_bw(key, current)
        after_list = [(fid, r) for fid, r in current if fid != df_id]
        after = self._group_total_bw(key, after_list)
        path_links = get_all_links_from_iie(key[1])   # key[1] is path_id
        return before - after, path_links

    def _marginal_cost_fn_for_tspec(self, l_max_bytes: int, r_min_Mbps: int):
        """Return a closure for AdmissionControl.decide() that uses marginal bundle cost."""
        def fn(path: Path, sr_Mbps: int) -> int:
            ingress_link = path.links[0]
            return self._bundle_marginal_bw(ingress_link, path.id, sr_Mbps,
                                            l_max_bytes, r_min_Mbps)
        return fn

    def _apply_flow_add(self, flow: Flow):
        ingress_links_list, _, _ = parse_path_id_iie(flow.path_id)
        ingress_link = ingress_links_list[0] if ingress_links_list else None
        all_links = get_all_links_from_iie(flow.path_id)

        if ingress_link and ingress_link in self.topo.links:
            # Bundle-aware: compute marginal scheduled bandwidth BEFORE updating state
            marginal = self._bundle_marginal_bw(
                ingress_link, flow.path_id, flow.sr_Mbps, flow.l_max_bytes, flow.r_min_Mbps
            )
            # Register this flow in its bundle group
            key = (ingress_link, flow.path_id, flow.sr_Mbps, flow.l_max_bytes)
            self.bundle_groups.setdefault(key, []).append((flow.df_id, flow.r_min_Mbps))
            self.flow_bundle_key[flow.df_id] = key
            # Reserve only the marginal bandwidth on every link in the path
            for l in all_links:
                self.residual_Mbps[l] -= marginal
                if self.residual_Mbps[l] < 0:
                    raise RuntimeError(f"Residual capacity negative on link {l}")
        else:
            # No recognised ingress — should not occur in normal operation
            for l in all_links:
                self.residual_Mbps[l] -= flow.sr_Mbps
                if self.residual_Mbps[l] < 0:
                    raise RuntimeError(f"Residual capacity negative on link {l}")

        self.flows.append(flow)
        if ingress_link:
            self.affected_ingresses.add(ingress_link)

    def _path_by_id(self, pid: str) -> Optional[Path]:
        # Extract all links from IIE format
        links = get_all_links_from_iie(pid)
        if not all(l in self.topo.links for l in links):
            return None
        lat = sum(self.topo.links[l].prop_latency_s for l in links)
        return Path(id=pid, links=links, total_prop_latency_s=lat, hop_count=len(links))

    def _log_link_allocation_fractions_before_pss(self, ingress_links: List[str]) -> None:
        scope = ', '.join(ingress_links)
        print(f"[INFO] Pre-PSS link allocation fractions (ingresses: {scope})")
        for lid in sorted(self.topo.links):
            link = self.topo.links[lid]
            capacity = float(link.capacity_Mbps)
            residual = float(self.residual_Mbps.get(lid, 0))
            allocated = max(capacity - residual, 0.0)
            fraction = (allocated / capacity) if capacity > 0 else 0.0
            print(
                f"[INFO]   {lid}: allocated={allocated:.0f}/{capacity:.0f} Mbps "
                f"(percent={fraction * 100:.2f}%)"
            )

    def _log_flow_rejection(self, tspec: TSpecR, reason: Optional[str]) -> None:
        dst_ids = tspec.get_dst_ids() if hasattr(tspec, "get_dst_ids") else tspec.dst_id
        print(f"[INFO] Flow rejected: {tspec.src_id} -> {dst_ids} (reason: {reason or 'unspecified'})")

    # Add a flow to the network using the admission control policy of choice
    def add_flow(self, tspec: TSpecR) -> Dict:
        """
        Add a flow to the network (via routing + admission control + resource allocation).
        Supports both unicast (single destination) and multicast (multiple destinations).
        """
        # WORKAROUND: FastAPI may have cached old schema that creates TSpecR with string dst_id
        # If dst_id is a string, convert to list. If it's already a list, use as-is.
        if isinstance(tspec.dst_id, str):
            # Old cached schema: normalize to list
            tspec.dst_id = [tspec.dst_id]

        # Note: unit_cost from request is IGNORED - it's always computed by the controller
        # based on reference_unit_cost and cost_policy. Users should not set unit_cost.

        # Handle multicast or unicast flow
        dst_ids = tspec.get_dst_ids()
        is_multicast = len(dst_ids) > 1

        if is_multicast:
            # Route multicast flow
            # print(f"[DEBUG] add_flow: routing multicast flow to {len(dst_ids)} destinations: {dst_ids}")
            result = self._route_multicast_flow(tspec, dst_ids)
            if not result.get("accepted"):
                self._log_flow_rejection(tspec, result.get("reason"))
                return result
            selected_path_id = result["path_id"]
            sr_Mbps = result["sr_Mbps"]
            # Create a Path object for consistency with unicast flow
            res = {
                "accepted": True,
                "selected_path": self._path_by_id(selected_path_id) or Path(
                    id=selected_path_id,
                    links=result.get("links", []),
                    total_prop_latency_s=0.0,
                    hop_count=len(result.get("links", []))
                ),
                "sr_Mbps": sr_Mbps
            }
        else:
            # Route unicast flow (traditional path)
            key = (tspec.src_id, dst_ids[0])
            candidates = self.routing_matrix.paths.get(key, [])

            # Set of ingress links the send traffic to each interior link
            ingress_cover_sets = {lid: set() for lid in self.topo.links if lid not in self.topo.ingress_links}

            endpoints = [n for n, d in self.topo.nodes.items() if d.kind == 'endpoint']
            for s in endpoints:
                for t in endpoints:
                    if s == t: continue
                    key = (s, t)
                    s_paths = self.routing_matrix.paths.get(key, [])
                    for p in s_paths:
                        for l in p.links:
                            if l in self.topo.ingress_links: continue
                            ingress_cover_sets[l].add(p.links[0])

            # Number of ingress links that send traffic to each interior link
            ingress_cover = {lid: len(ingress_cover_sets[lid]) for lid in self.topo.links if lid not in self.topo.ingress_links}

            adm = AdmissionControl(self.topo, slot_duration_s=self.cfg.slot_duration_s, mode=self.cfg.admission_mode,
                                   conservative_cac_max_path_hops=self.cfg.conservative_cac_max_path_hops,
                                   conservative_cac_max_link_latency_s=self.cfg.conservative_cac_max_link_latency_s,
                                   bandwidth_overhead=self.cfg.bandwidth_overhead)
            res = adm.decide(tspec, candidates, self.residual_Mbps, ingress_cover,
                             marginal_cost_fn=self._marginal_cost_fn_for_tspec(
                                 tspec.L_Max_bytes, tspec.R_Min_Mbps))

            # Return if admission control failed
            if not res.get("accepted") or not res.get("selected_path") or res.get("sr_Mbps") is None:
                reason = res.get("reason")
                self._log_flow_rejection(tspec, reason)
                return {"accepted": False, "reason": reason}

        # Flow id assignment: if not already given (new flow), generate it randomly
        if not tspec.DF_ID or tspec.DF_ID == "0":
            fid = str(uuid.uuid4())

            # DEBUG
            # print("random flow ID assigned: {}".format(fid))
            # END DEBUG
        else:
            fid = tspec.DF_ID

        # If fid already exists, a new one is tried until it is unique
        for loc_flow in self.flows:

            # DEBUG
            #print("fid: {}, loc_flow ID: {}".format(fid, loc_flow.df_id))
            # END DEBUG
            if fid == loc_flow.df_id:
                fid = str(uuid.uuid4())

                # DEBUG
                # print("fid was just reassigned: {}".format(fid))
                # END DEBUG

        # Determine final unit_cost based on cost policy
        if self.cfg.cost_policy == "congestion_cost":
            # Compute cost based on congestion BEFORE applying the flow
            # The congestion factor is multiplied by reference_unit_cost
            congestion_factor = self.compute_congestion_cost(
                res["selected_path"].id,
                res["sr_Mbps"]
            )
            final_unit_cost = self.runtime_reference_unit_cost * congestion_factor
            # print(f"[DEBUG] add_flow: congestion_cost policy - reference={self.runtime_reference_unit_cost:.6f}, "
            #       f"congestion_factor={congestion_factor:.6f}, final={final_unit_cost:.6f}")
        else:
            # manual_cost: always use reference_unit_cost (user cannot set unit_cost per-flow)
            final_unit_cost = self.runtime_reference_unit_cost
            # print(f"[DEBUG] add_flow: manual_cost policy - using reference_unit_cost = {final_unit_cost}")

        # Get VLAN ID for multicast flows (already computed in _route_multicast_flow)
        vlan_id = result.get("vlan_id") if is_multicast else None

        # Allocate a flow instance and fill out all fields
        flow = Flow(
            df_id=fid, src_id=tspec.src_id, dst_id=tspec.dst_id, pcs=tspec.PCS_R,
            path_id=res["selected_path"].id, sr_Mbps=res["sr_Mbps"],
            l_max_bytes=tspec.L_Max_bytes, b_ref_bytes=tspec.B_Ref_bytes,
            d_max_ns=tspec.D_Max_ns,
            unit_cost=final_unit_cost,
            r_min_Mbps=tspec.R_Min_Mbps,
            vlan_id=vlan_id  # Include VLAN ID for multicast flows
        )

        # Update all state variables affected by the introduction of the new flow
        self._apply_flow_add(flow)

        # Recompute routing matrix if using load-aware policy
        # This ensures subsequent flows see updated residual bandwidth
        if self._is_load_aware_routing_policy():
            self._recompute_routing_matrix()

        # PSS configuration files are written when /pss/push is called.

        #pss_by_ing = self.compute_all_pss()
        # The following does not seem to be needed:
        #bytes_per_slot = int(min(self.topo.links[l].capacity_Mbps for l in res["selected_path"].links) * self.cfg.slot_duration_s / 8.0)
        tspec_c = {
            "DF_ID": fid,
            "NPL": res["selected_path"].id,
            "PCS_C": tspec.PCS_R,
            "SR_Mbps": res["sr_Mbps"],
            "MBS_bytes": tspec.B_Ref_bytes,
            "unit_cost": final_unit_cost
        }
        return {"accepted": True, "TSPEC_C": tspec_c}

    def add_flows_batch(self, tspec: TSpecR, count: int) -> Dict:
        """
        Add multiple identical flows sequentially.
        If one flow fails admission, stop attempting remaining flows.

        Args:
            tspec: Flow specification (without count field)
            count: Number of identical flows to add

        Returns:
            Dict with:
                - accepted_flows: List of successfully added flow IDs and TSpec_Cs
                - rejected_count: Number of flows not admitted
                - total_requested: Total number requested
                - reason: Reason for stopping (if applicable)
        """
        accepted_flows = []
        rejected_count = 0
        rejection_reason = None

        # Get unit_cost safely
        tspec_data = tspec.model_dump()
        unit_cost = tspec_data.get('unit_cost', 0.0)

        # WORKAROUND: FastAPI may have cached old schema that sends dst_id as string
        # Normalize dst_id to always be a list
        dst_id_normalized = tspec.dst_id if isinstance(tspec.dst_id, list) else [tspec.dst_id]

        for i in range(count):
            # Create a copy of tspec for this iteration (ensuring unique DF_ID)
            flow_tspec = TSpecR(
                DF_ID=None,  # Always generate new ID
                src_id=tspec.src_id,
                dst_id=dst_id_normalized,  # Use normalized list
                PCS_R=tspec.PCS_R.copy(),
                D_Max_ns=tspec.D_Max_ns,
                L_Max_bytes=tspec.L_Max_bytes,
                B_Ref_bytes=tspec.B_Ref_bytes,
                R_Min_Mbps=tspec.R_Min_Mbps,
                unit_cost=unit_cost
            )

            # Attempt to add the flow
            result = self.add_flow(flow_tspec)

            if result.get("accepted", False):
                # Flow accepted
                accepted_flows.append({
                    "flow_number": i + 1,
                    "DF_ID": result["TSPEC_C"]["DF_ID"],
                    "TSPEC_C": result["TSPEC_C"]
                })
            else:
                # Flow rejected - stop trying remaining flows
                rejection_reason = result.get("reason", "Unknown reason")
                rejected_count = count - i  # Current flow + all remaining
                print(f"Batch flow addition stopped at flow {i+1}/{count}: {rejection_reason}")
                break

        return {
            "accepted_flows": accepted_flows,
            "accepted_count": len(accepted_flows),
            "rejected_count": rejected_count,
            "total_requested": count,
            "reason": rejection_reason,
            "success": len(accepted_flows) > 0
        }

    def remove_flow(self, df_id: str) -> Dict:
        # DEBUG
        # print("remove flow {}".format(df_id))
        # print("flows: {}".format(self.flows))
        # END DEBUG

        flow = next((f for f in self.flows if f.df_id == df_id), None)
        if not flow:
            return {"removed": False, "reason": "Flow not found"}

        # Update running cost one final time before removing
        self.update_flow_cost(flow)
        final_running_cost = flow.running_cost

        # Track the affected ingress link before removing - extract from IIE format
        ingress_links, _, _ = parse_path_id_iie(flow.path_id)
        if ingress_links:
            self.affected_ingresses.add(ingress_links[0])

        # Bundle-aware: compute the bandwidth freed by removing this flow
        bw_freed, path_links = self._bundle_removal_bw(df_id)

        # Remove flow from bundle group state
        bkey = self.flow_bundle_key.pop(df_id, None)
        if bkey is not None and bkey in self.bundle_groups:
            self.bundle_groups[bkey] = [(fid, r) for fid, r in self.bundle_groups[bkey]
                                        if fid != df_id]
            if not self.bundle_groups[bkey]:
                del self.bundle_groups[bkey]

        # Restore freed bandwidth on all links in the path
        for l in path_links:
            if l in self.residual_Mbps:
                self.residual_Mbps[l] += bw_freed

        self.flows = [f for f in self.flows if f.df_id != df_id]

        # Recompute routing matrix if using load-aware policy
        if self._is_load_aware_routing_policy():
            self._recompute_routing_matrix()

        # PSS configuration files are written when /pss/push is called.
        return {"removed": True, "final_running_cost": final_running_cost}

    def get_flow(self, df_id: str) -> Optional[Flow]:
        """Get a flow by its DF_ID"""
        return next((f for f in self.flows if f.df_id == df_id), None)

    def compute_congestion_cost(self, path_id: str, service_rate_mbps: int) -> float:
        """
        Compute congestion factor for a flow based on spare bandwidth utilization.

        The congestion factor indicates how much of the spare bandwidth this flow will use.
        This factor is multiplied by reference_unit_cost to get the final unit_cost.

        Formula:
        - congestion_factor = service_rate / min_spare_bandwidth
        - congestion_factor is capped at 1.0 (if > 1.0, admission control should reject)
        - unit_cost = reference_unit_cost * congestion_factor

        Args:
            path_id: The path ID in IIE format (e.g., "u01l01_l01^l01_s01-s01_l02^l02_u01l02")
                    Format: ingress_links^interior_links^egress_links
            service_rate_mbps: The service rate allocated to the flow

        Returns:
            The congestion factor (0.0 to 1.0) - a multiplier for reference_unit_cost
        """
        # Parse IIE format path_id: ingress^interior^egress
        sections = path_id.split('^')

        if len(sections) == 3:
            # Standard IIE format
            interior_section = sections[1]  # Middle section contains interior links
            if interior_section:
                # Interior section may contain multiple links separated by hyphens
                inner_links = interior_section.split('-')
            else:
                # No interior links (direct ingress->egress path)
                inner_links = []
        elif len(sections) == 1:
            # Legacy format or single-link path - split on hyphens
            links = path_id.split('-')
            if len(links) <= 2:
                inner_links = links  # Use all links for short paths
            else:
                inner_links = links[1:-1]  # Skip first and last
        else:
            # Unexpected format - use default congestion
            print(f"[WARNING] compute_congestion_cost: unexpected path_id format: {path_id}")
            return 0.1

        if not inner_links:
            # No interior links - could be direct endpoint-to-endpoint connection
            # Use ingress and egress links for congestion calculation
            if len(sections) == 3:
                ingress_section = sections[0]
                egress_section = sections[2]
                # Combine ingress and egress links
                inner_links = []
                if ingress_section:
                    inner_links.extend(ingress_section.split('-'))
                if egress_section:
                    inner_links.extend(egress_section.split('-'))

            if not inner_links:
                # Still no links - use minimal congestion factor
                return 0.1

        # Find minimum spare bandwidth among inner links
        min_spare_bandwidth = float('inf')

        for link_id in inner_links:
            if link_id in self.residual_Mbps:
                spare_bandwidth = self.residual_Mbps[link_id]
                if spare_bandwidth < min_spare_bandwidth:
                    min_spare_bandwidth = spare_bandwidth

        # Handle edge cases
        if min_spare_bandwidth == float('inf'):
            # No valid links found in residual_Mbps - use default congestion factor
            print(f"[WARNING] compute_congestion_cost: no valid links found in residual_Mbps for path {path_id}")
            return 0.1

        if min_spare_bandwidth <= 0:
            # No spare bandwidth - flow should have been rejected by admission control
            # If we reach here, something is wrong, but return max factor
            return 1.0

        # Compute congestion factor: ratio of service rate to spare bandwidth
        congestion_factor = float(service_rate_mbps) / float(min_spare_bandwidth)

        # Cap at 1.0 - flows with factor > 1.0 should be rejected by admission control
        congestion_factor = min(congestion_factor, 1.0)

        # print(f"[DEBUG] compute_congestion_cost: path_id={path_id}")
        # print(f"[DEBUG] compute_congestion_cost: sections={sections}, inner_links={inner_links}")
        # print(f"[DEBUG] compute_congestion_cost: service_rate={service_rate_mbps}, min_spare={min_spare_bandwidth}, factor={congestion_factor:.6f}")

        return congestion_factor

        # Compute congestion factor: ratio of service rate to spare bandwidth
        congestion_factor = float(service_rate_mbps) / float(min_spare_bandwidth)

        # Cap at 1.0: if factor > 1.0, admission control should have rejected the flow
        # but if we somehow reach here, cap it at 1.0 (max cost)
        congestion_factor = min(congestion_factor, 1.0)

        return congestion_factor

    def update_flow_cost(self, flow: Flow) -> None:
        """Update running cost of a flow based on time elapsed."""
        import time
        if flow.time_last_update is None:
            # Cost tracking not initialized yet
            return
        current_time = time.time()
        time_elapsed = current_time - flow.time_last_update
        flow.running_cost += time_elapsed * flow.unit_cost
        flow.time_last_update = current_time

    def initialize_flow_cost_tracking(self, flow: Flow) -> None:
        """Initialize cost tracking when PSS profile is first pushed."""
        import time
        if flow.time_last_update is None:
            flow.time_last_update = time.time()

    def modify_flow(self, df_id: str, tspec: TSpecR) -> Dict:
        # DEBUG
        # print("modify flow - df_id {}  tspec {}".format(df_id, tspec))
        # END DEBUG

        # Get unit_cost safely
        tspec_data = tspec.model_dump()
        unit_cost = tspec_data.get('unit_cost', 0.0)

        # Validate new unit_cost
        if unit_cost < 0:
            return {"accepted": False, "reason": "Unit cost cannot be negative"}

        # Get existing flow and update its cost
        existing_flow = self.get_flow(df_id)
        if not existing_flow:
            return {"accepted": False, "reason": "Flow to modify not found"}

        # Update running cost before modification
        self.update_flow_cost(existing_flow)
        current_running_cost = existing_flow.running_cost

        # Forcing the flow id in the TSpec to match that in the argument
        tspec.DF_ID = df_id

        removal_outcome = self.remove_flow(df_id)
        if removal_outcome["removed"]:
            result = self.add_flow(tspec)
            if result.get("accepted"):
                result["current_running_cost"] = current_running_cost
            return result
        else:
            return {"accepted": False, "reason": "Flow to modify not found"}

    def compute_single_pss(self, new_flow: Flow) -> Dict[str, IngressRASPlan]:
        """
        Build the PSS only for the ingress link of the last added/modified/removed flow
        """
        # Identify the ingress link of the flow from IIE format
        ingress_links, _, _ = parse_path_id_iie(new_flow.path_id)
        if not ingress_links:
            raise ValueError(f"No ingress link found in path_id: {new_flow.path_id}")
        ing_lid = ingress_links[0]

        self._log_link_allocation_fractions_before_pss([ing_lid])

        # Collect information on all flows of the ingress link
        flow_info_ingress_link: Dict[str, Dict[str, object]] = {}
        for f in self.flows:
            # Check if flow uses this ingress link
            f_ingress_links, _, _ = parse_path_id_iie(f.path_id)
            if not f_ingress_links or f_ingress_links[0] != ing_lid:
                continue

            # Extract all links in traversal order from IIE format
            flow_link_ids = get_all_links_from_iie(f.path_id)
            flow_info_ingress_link[f.df_id] = dict(
                sr_Mbps=f.sr_Mbps,
                l_max_bytes=f.l_max_bytes,
                path_link_ids=flow_link_ids,
                r_min_Mbps=f.r_min_Mbps,
            )
        # DEBUG
        #print("In: compute_single_pss - flow_info_ingress_link: {}".format(flow_info_ingress_link))
        # END DEBUG

        # Not clear this 'plans' dictionary collects...
        # IngressRASPlan is a per-ingress-link RAS instance, with the
        # PSS and the following bundle items:
        # bundle_id_by_flow: key is fid, value is bid
        # bundle_members: key is bid, value is list of fids
        # bundle_path_links: key is bid, value is list of lids in path
        # Maybe the key is the ingress link id?
        plans: Dict[str, IngressRASPlan] = {}

        # Computing the virtual link rates for the auxiliary scheduler of the ingress link.
        # Use bundle-group state so the rate per link equals the sum of scheduled bundle
        # effective rates (not the sum of per-flow sr_Mbps).
        link_virtual_rates: Dict[str, int] = {}
        for key, flow_list in self.bundle_groups.items():
            grp_ingress, grp_path_id, _, _ = key
            if grp_ingress != ing_lid:
                continue
            grp_bw = self._group_total_bw(key, flow_list)
            for l in get_all_links_from_iie(grp_path_id):
                link_virtual_rates[l] = link_virtual_rates.get(l, 0) + grp_bw

        # DEBUG
        # print("In: compute_single_pss - link_virtual_rates: {}".format(link_virtual_rates))
        # END DEBUG

        # Get the capacity of the link
        link_capacity = self.topo.links[ing_lid].capacity_Mbps

        # DEBUG
        # print("In: compute_single_pss - ingress_link: {}  link_capacity: {}".format(ing_lid, link_capacity))
        # END DEBUG

        plan = build_ingress_pss(
            ingress_link=ing_lid,
            link_capacity_Mbps=link_capacity,
            scheduling_policy=self.cfg.scheduling_policy,
            slot_duration_s=self.cfg.slot_duration_s,
            period_slots=self.cfg.pss_period_slots,
            flow_info=flow_info_ingress_link,
            link_virtual_rates=link_virtual_rates,
            topology=self.topo,
            bandwidth_overhead=self.cfg.bandwidth_overhead,
        )

        # DEBUG
        # print("In: compute_single_pss - plan: {}".format(plan))
        # END DEBUG

        return {ing_lid: plan}

    def compute_all_pss(self) -> Dict[str, IngressRASPlan]:
        """
        Build PSS for each ingress with v0.04 bundle semantics.
        We pass per-flow info to the PSS builder so it can pack bundles.
        """
        return self.compute_pss_for_ingresses(self.topo.ingress_links)

    def compute_pss_for_ingresses(self, ingress_links: List[str]) -> Dict[str, IngressRASPlan]:
        """
        Build PSS for specified ingress links only.
        This is more efficient than compute_all_pss when only a few ingresses have changed.

        Args:
            ingress_links: List of ingress link IDs to compute PSS for

        Returns:
            Dictionary mapping ingress link ID to IngressRASPlan
        """
        self._log_link_allocation_fractions_before_pss(ingress_links)

        # Build per-flow info: df_id -> {sr_Mbps, l_max_bytes, path_link_ids}
        flow_info_all: Dict[str, Dict[str, object]] = {}
        for f in self.flows:
            # Get traversal-order links from path object in routing matrix
            path_obj = self._path_by_id(f.path_id)
            if path_obj:
                link_ids = path_obj.links
            else:
                # Fallback: extract from IIE format path_id
                # Note: get_all_links_from_iie is already imported at module level
                link_ids = get_all_links_from_iie(f.path_id)

            flow_info_all[f.df_id] = dict(
                sr_Mbps=f.sr_Mbps,
                l_max_bytes=int(f.l_max_bytes),
                path_link_ids=link_ids,
                r_min_Mbps=f.r_min_Mbps,
            )

        # DEBUG
        # print("In: compute_pss_for_ingresses - flow_info_all: {}".format(flow_info_all))
        # END DEBUG

        plans: Dict[str, IngressRASPlan] = {}

        t_total_start = time.perf_counter()
        for ing in ingress_links:
            link_cap = self.topo.links[ing].capacity_Mbps if ing in self.topo.links else 1e3

            # filter flow_info for flows that originate at this ingress
            flow_info_ing = {df: info for df, info in flow_info_all.items()
                             if info["path_link_ids"] and info["path_link_ids"][0] == ing}

            # Compute link_virtual_rates PER-INGRESS from bundle-group state so the rate
            # per link equals the sum of scheduled bundle effective rates.
            link_virtual_rates: Dict[str, int] = {}
            for key, flow_list in self.bundle_groups.items():
                grp_ingress, grp_path_id, _, _ = key
                if grp_ingress != ing:
                    continue
                grp_bw = self._group_total_bw(key, flow_list)
                for l in get_all_links_from_iie(grp_path_id):
                    link_virtual_rates[l] = link_virtual_rates.get(l, 0) + grp_bw

            # DEBUG
            print("In: compute_pss_for_ingresses - ingress: {}  link_virtual_rates: {}".format(ing, link_virtual_rates))
            # END DEBUG

            t_ing_start = time.perf_counter()
            plans[ing] = build_ingress_pss(
                ingress_link=ing,
                link_capacity_Mbps=link_cap,
                scheduling_policy=self.cfg.scheduling_policy,
                slot_duration_s=self.cfg.slot_duration_s,
                period_slots=self.cfg.pss_period_slots,
                flow_info=flow_info_ing,
                link_virtual_rates=link_virtual_rates,
                topology=self.topo,
                bandwidth_overhead=self.cfg.bandwidth_overhead,
            )
            t_ing_elapsed = time.perf_counter() - t_ing_start
            print(f"[INFO] compute_pss_for_ingresses: ingress {ing} — {len(flow_info_ing)} flows — computed in {t_ing_elapsed*1000:.1f} ms")

        t_total_elapsed = time.perf_counter() - t_total_start
        print(f"[INFO] compute_pss_for_ingresses: {len(ingress_links)} ingress(es) computed in {t_total_elapsed*1000:.1f} ms total")

        # DEBUG
        # print("In: compute_pss_for_ingresses - plans: {}".format(plans))
        # END DEBUG

        return plans

    async def _plans_to_profiles(self, plans: Dict[str, 'IngressRASPlan']):
        """Build RAS IngressProfile models for each ingress from PSS plans and current flows."""
        profiles: Dict[str, IngressProfile] = {}
        flows_by_id = {f.df_id: f for f in self.flows}

        for ing, plan in plans.items():
            slots = plan.pss.slots
            bytes_per_slot = plan.pss.bytes_per_slot
            included: List[FlowProfile] = []

            # Build path_number mappings based on scheduling policy
            if self.cfg.scheduling_policy in ["ras", "plain_hierarchical"]:
                # path_number = ordinal of unique path_id
                path_to_number: Dict[str, int] = {}
                path_counter = 1

                for df_id in plan.bundle_id_by_flow.keys():
                    f = flows_by_id[df_id]
                    path_id = f.path_id
                    if path_id not in path_to_number:
                        path_to_number[path_id] = path_counter
                        path_counter += 1

            elif self.cfg.scheduling_policy == "flat":
                # For flat: path_number = ordinal of bundle (starting from 1)
                bundle_to_number: Dict[str, int] = {}
                bundle_counter = 1

                for bundle_id in plan.bundle_members.keys():
                    if bundle_id not in bundle_to_number:
                        bundle_to_number[bundle_id] = bundle_counter
                        bundle_counter += 1

            for df_id in plan.bundle_id_by_flow.keys():
                f = flows_by_id[df_id]
                # Get src_ip and dst_ip from topology node definitions based on src_id and dst_id
                src_node = self.topo.nodes.get(f.src_id)
                src_ip = src_node.ip_address if src_node else ""

                # For dst_ip, assign multicast IP addresses for multicast flows
                dst_ids = f.get_dst_ids()
                is_multicast = len(dst_ids) > 1

                if is_multicast:
                    # For multicast flows, use valid multicast IP addresses (224.0.0.x range)
                    # Use flow's VLAN ID to generate unique multicast address if available
                    if f.vlan_id:
                        # Parse VLAN ID to use as part of multicast address
                        try:
                            if isinstance(f.vlan_id, str) and f.vlan_id.startswith("0x"):
                                vlan_int = int(f.vlan_id, 16)
                            elif isinstance(f.vlan_id, int):
                                vlan_int = f.vlan_id
                            else:
                                vlan_int = int(f.vlan_id)
                            # Map VLAN to multicast address: 224.0.0.x where x = vlan_id mod 256
                            # Start from 224.0.0.1 (all hosts) as base
                            multicast_host = max(1, vlan_int % 256)
                            dst_ip = f"224.0.0.{multicast_host}"
                        except (ValueError, TypeError):
                            # Default to 224.0.0.1 (all hosts in network domain)
                            dst_ip = "224.0.0.1"
                    else:
                        # No VLAN ID available, use default multicast address
                        dst_ip = "224.0.0.1"
                else:
                    # Unicast flow - use the destination node's IP address
                    first_dst_id = dst_ids[0] if dst_ids else ""
                    dst_node = self.topo.nodes.get(first_dst_id)
                    dst_ip = dst_node.ip_address if dst_node else ""

                # Get vlan_id - for multicast flows, use the vlan_id already assigned to the flow
                # For unicast flows, look up from topology paths
                if f.vlan_id:
                    # Multicast flow with pre-assigned VLAN ID
                    if isinstance(f.vlan_id, str):
                        vlan_id = f.vlan_id
                    elif isinstance(f.vlan_id, int):
                        vlan_id = hex(f.vlan_id)
                    else:
                        vlan_id = "0x0"
                else:
                    # Unicast flow - look up from topology paths based on flow's path_id
                    vlan_id_str = self.topo.paths.get(f.path_id, "0x0")
                    # Convert to integer first, then to hex string format
                    # Handles both hex ("0x1") and decimal ("1") input formats
                    try:
                        # Try parsing as hex first
                        if isinstance(vlan_id_str, str) and vlan_id_str.startswith("0x"):
                            vlan_id_int = int(vlan_id_str, 16)
                        else:
                            # Otherwise parse as decimal
                            vlan_id_int = int(vlan_id_str)
                        # Convert to hex format (e.g., "0xb")
                        vlan_id = hex(vlan_id_int)
                    except (ValueError, TypeError):
                        vlan_id = "0x0"

                # Get vlan_pri from flow's PCS_R vlanPrio field
                vlan_pri = None
                if f.pcs:
                    vlan_pri_str = f.pcs.get("vlanPrio") or f.pcs.get("vlanPri") or f.pcs.get("vlan_pri")
                    if vlan_pri_str is not None:
                        try:
                            vlan_pri = int(vlan_pri_str)
                        except (ValueError, TypeError):
                            pass
                # Default to 0 if not specified
                if vlan_pri is None:
                    vlan_pri = 0

                # Compute path_number based on scheduling policy
                path_number = None
                if self.cfg.scheduling_policy in ["ras", "plain_hierarchical"]:
                    # Path number based on path_id
                    path_number = path_to_number.get(f.path_id, 1)
                elif self.cfg.scheduling_policy == "flat":
                    # Path number based on bundle
                    bundle_id = plan.bundle_id_by_flow.get(df_id)
                    if bundle_id:
                        path_number = bundle_to_number.get(bundle_id, 1)

                included.append(FlowProfile(
                    df_id=df_id,
                    sr_Mbps=f.sr_Mbps,
                    path_link_ids=get_all_links_from_iie(f.path_id),
                    src_ip=src_ip,
                    dst_ip=dst_ip,
                    latency_ns=f.d_max_ns,
                    l_max_bytes=f.l_max_bytes,
                    vlan_id=vlan_id,
                    vlan_pri=vlan_pri,
                    path_number=path_number,
                ))
            profiles[ing] = IngressProfile(
                ingress_link_id=ing,
                slot_duration_s=self.cfg.slot_duration_s,
                period_slots=plan.pss.period_slots,
                bytes_per_slot=bytes_per_slot,
                slots=slots,
                # bundle metadata for RAS: bundle -> members, and per-flow bundle mapping
                bundle_members=plan.bundle_members,
                bundle_id_by_flow=plan.bundle_id_by_flow,
                bundle_effective_rates=plan.bundle_effective_rates,
                flows=included,
            )
        return profiles

    def _pss_output_dir(self) -> FilePath:
        """Directory for *-TM.cfg and *-STREAMS.cfg, from the controller config.

        A relative path is resolved from the process working directory.
        """
        raw = self.cfg.pss_output_dir or "pss-output"
        folder = FilePath(raw)
        if not folder.is_absolute():
            folder = FilePath.cwd() / folder
        return folder

    def _write_profiles(self, profiles: Dict[str, IngressProfile], only_changed: bool) -> Dict[str, dict]:
        """Write cfg files for the given ingress profiles. Returns one result per ingress."""
        output_dir = self._pss_output_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        logging.info("Writing PSS configuration files to %s", output_dir)

        results: Dict[str, dict] = {}
        changed_count = 0
        unchanged_count = 0
        for ing, prof in profiles.items():
            if only_changed and not self._profile_changed(ing, prof):
                unchanged_count += 1
                results[ing] = {
                    "pushed": False,
                    "written": False,
                    "changed": False,
                    "reason": "no changes detected",
                }
                continue
            try:
                files = write_endpoint_configs(output_dir, prof)
            except Exception as exc:
                logging.error("Failed to write PSS files for %s: %s", ing, exc)
                results[ing] = {
                    "pushed": False,
                    "written": False,
                    "changed": True,
                    "error": str(exc),
                }
                continue
            self.last_pushed_profiles[ing] = prof
            for flow_profile in prof.flows:
                flow = self.get_flow(flow_profile.df_id)
                if flow:
                    self.initialize_flow_cost_tracking(flow)
            changed_count += 1
            results[ing] = {
                "pushed": True,
                "written": True,
                "changed": True,
                "files": files,
            }
            logging.info(
                "Wrote %s and %s",
                files["tm_cfg"],
                files["streams_cfg"],
            )

        if only_changed:
            results["_summary"] = {
                "total_profiles": len(profiles),
                "changed": changed_count,
                "unchanged": unchanged_count,
                "pushed": changed_count,
                "output_dir": str(output_dir),
            }
        logging.info(
            "PSS files: %s written, %s unchanged, directory %s",
            changed_count, unchanged_count, output_dir,
        )
        return results

    async def push_all_pss(self) -> Dict[str, dict]:
        """Compute PSS for every ingress and write *-TM.cfg and *-STREAMS.cfg."""
        plans = self.compute_all_pss()
        profiles = await self._plans_to_profiles(plans)
        return self._write_profiles(profiles, only_changed=False)

    def _profile_changed(self, ing: str, new_profile: 'IngressProfile') -> bool:
        """Check if a profile has changed compared to the last pushed version."""
        if ing not in self.last_pushed_profiles:
            return True  # No previous profile, so it's changed

        old_profile = self.last_pushed_profiles[ing]

        # Compare profiles by converting to dict and comparing
        # We exclude timestamps or any dynamic fields if they exist
        new_dict = new_profile.model_dump()
        old_dict = old_profile.model_dump()

        # Compare the essential fields
        return new_dict != old_dict

    async def push_changed_pss(self) -> Dict[str, dict]:
        """Write cfg files only for ingresses whose profile changed since the last write."""
        if self.affected_ingresses:
            ingresses_to_compute = list(self.affected_ingresses)
            logging.info("Writing PSS for %s affected ingress(es)", len(ingresses_to_compute))
        else:
            ingresses_to_compute = list(self.topo.ingress_links)
            logging.info("No affected ingresses tracked; writing PSS for all %s ingresses", len(ingresses_to_compute))

        plans = self.compute_pss_for_ingresses(ingresses_to_compute)
        self.affected_ingresses.clear()
        profiles = await self._plans_to_profiles(plans)
        return self._write_profiles(profiles, only_changed=True)

    async def modify_scheduling_policy(self, new_policy: str) -> Dict:
        """
        Modify the scheduling policy at runtime.

        If the policy changes, recompute PSS for all ingress links and write the cfg files.
        If the policy is the same, do nothing.

        Args:
            new_policy: The new scheduling policy ("ras", "flat", or "plain_hierarchical")

        Returns:
            Dictionary with status information
        """
        current_policy = self.cfg.scheduling_policy

        # Check if the policy is actually changing
        if new_policy == current_policy:
            return {
                "changed": False,
                "message": f"Scheduling policy is already set to '{current_policy}'. No action taken.",
                "current_policy": current_policy
            }

        # Update the configuration
        old_policy = current_policy
        self.cfg.scheduling_policy = new_policy

        print(f"Scheduling policy changed from '{old_policy}' to '{new_policy}'")
        print("Recomputing PSS for all ingress links with new policy...")

        # Recompute PSS for all ingresses and write the cfg files
        try:
            push_results = await self.push_all_pss()

            successful = sum(1 for r in push_results.values() if isinstance(r, dict) and r.get('pushed', False))
            total = len(push_results)

            return {
                "changed": True,
                "old_policy": old_policy,
                "new_policy": new_policy,
                "message": f"Scheduling policy changed from '{old_policy}' to '{new_policy}'",
                "pss_recomputed": True,
                "pss_pushed": True,
                "push_summary": {
                    "total_ingresses": total,
                    "successful_pushes": successful,
                    "failed_pushes": total - successful
                },
                "push_details": push_results
            }
        except Exception as e:
            # Rollback the configuration change on error
            self.cfg.scheduling_policy = old_policy
            error_msg = f"Failed to push PSS after policy change. Configuration rolled back to '{old_policy}'. Error: {str(e)}"
            print(error_msg)
            raise RuntimeError(error_msg)

    def _minimum_tree_new_links(self, path_links: List[str], multicast_links: set[str]) -> Tuple[List[str], int]:
        """Return the new-link suffix for a candidate path and the shared-prefix length."""
        shared_prefix_len = 0
        for link in path_links:
            if link in multicast_links:
                shared_prefix_len += 1
            else:
                break
        return path_links[shared_prefix_len:], shared_prefix_len

    def _required_sr_for_path(self, adm: AdmissionControl, tspec: TSpecR, path: Path,
                              ingress_cover: Dict[str, int]) -> Optional[int]:
        """Compute the shaping rate required for one candidate path without checking capacity."""
        if adm.mode == 'exact':
            d_net = adm._path_D_net_bound(path, ingress_cover)
        else:
            n_ingress_total = len(self.topo.ingress_links)
            d_net = adm._domain_upper_bound_D_net(
                max_path_hops=adm.cons_max_hops,
                n_ingress=n_ingress_total,
                d_max=adm.cons_max_link_latency_s,
            )

        c_mbps = self.topo.links[path.links[0]].capacity_Mbps
        r_req_mbps = adm.required_rate_for_latency(
            B_bytes=tspec.B_Ref_bytes,
            C_Mbps=c_mbps,
            D_target_s=tspec.D_Max_ns / 1e9,
            D_net_s=d_net,
        )
        if r_req_mbps is None:
            return None
        return adm.reserved_rate_Mbps(tspec.R_Min_Mbps, r_req_mbps)

    def _route_multicast_flow_minimum_tree(self, tspec: TSpecR, dst_ids: List[str]) -> Dict:
        """
        Route a multicast flow by incrementally growing a source-rooted tree.

        Candidate unicast paths are ordered by the same load-aware rule used for
        unicast routing, recomputed against a working residual state as the tree
        grows. For each new destination, the selected candidate is the feasible one
        that adds the fewest new links to the current tree; the unicast-defined
        order is used as a tie-breaker.
        """
        multicast_links = set()
        ingress_links = set()
        interior_links = set()
        egress_links = set()

        sr_Mbps = None
        working_residual = self.residual_Mbps.copy()

        ingress_cover_sets = {lid: set() for lid in self.topo.links if lid not in self.topo.ingress_links}
        endpoints = [n for n, d in self.topo.nodes.items() if d.kind == 'endpoint']
        for s in endpoints:
            for t in endpoints:
                if s == t:
                    continue
                key = (s, t)
                s_paths = self.routing_matrix.paths.get(key, [])
                for p in s_paths:
                    for l in p.links:
                        if l in self.topo.ingress_links:
                            continue
                        ingress_cover_sets[l].add(p.links[0])
        ingress_cover = {lid: len(ingress_cover_sets[lid]) for lid in self.topo.links if lid not in self.topo.ingress_links}

        adm = AdmissionControl(self.topo, slot_duration_s=self.cfg.slot_duration_s, mode=self.cfg.admission_mode,
                               conservative_cac_max_path_hops=self.cfg.conservative_cac_max_path_hops,
                               conservative_cac_max_link_latency_s=self.cfg.conservative_cac_max_link_latency_s,
                               bandwidth_overhead=self.cfg.bandwidth_overhead)
        policy = HighestSpareMinLinkPolicy(max_path_num=self.cfg.routing_max_path_num,
                                           max_hop_num=self.cfg.routing_max_hop_num,
                                           max_latency_num_us=self.cfg.routing_max_latency_num_us)

        for dst_id in dst_ids:
            candidates = policy.order_paths_for_pair(self.topo, working_residual, tspec.src_id, dst_id, k=8)
            if not candidates:
                return {"accepted": False, "reason": f"No paths available from {tspec.src_id} to {dst_id}"}

            scored_candidates = []
            for order_idx, path in enumerate(candidates):
                new_links, shared_prefix_len = self._minimum_tree_new_links(path.links, multicast_links)
                score = (len(new_links), -shared_prefix_len, order_idx)
                scored_candidates.append((score, path, new_links))

            if not scored_candidates:
                return {"accepted": False, "reason": f"No feasible path found for destination {dst_id}"}

            best_score = min(score for score, _, _ in scored_candidates)
            best_reuse_candidates = [item for item in scored_candidates if item[0] == best_score]

            best_choice = None
            for _, path, new_links in best_reuse_candidates:
                required_sr = self._required_sr_for_path(adm, tspec, path, ingress_cover)
                if required_sr is None:
                    continue

                candidate_sr = required_sr if sr_Mbps is None else max(sr_Mbps, required_sr)
                delta_sr = candidate_sr - (sr_Mbps or 0)
                if any(working_residual.get(l, 0) < candidate_sr for l in new_links):
                    continue

                best_choice = (path, candidate_sr, delta_sr, new_links)
                break

            if best_choice is None:
                return {"accepted": False, "reason": f"No feasible minimum-tree path found for destination {dst_id}"}

            chosen_path, chosen_sr, delta_sr, chosen_new_links = best_choice

            if delta_sr > 0:
                for l in multicast_links:
                    working_residual[l] -= delta_sr

            sr_Mbps = chosen_sr

            for l in chosen_new_links:
                working_residual[l] -= sr_Mbps

            for link in chosen_path.links:
                multicast_links.add(link)
                if link in self.topo.ingress_links:
                    ingress_links.add(link)
                elif link in self.topo.egress_links:
                    egress_links.add(link)
                else:
                    interior_links.add(link)

        ingress_str = '-'.join(sorted(ingress_links)) if ingress_links else ""
        interior_str = '-'.join(sorted(interior_links)) if interior_links else ""
        egress_str = '-'.join(sorted(egress_links)) if egress_links else ""
        multicast_path_id = f"{ingress_str}^{interior_str}^{egress_str}"

        vlan_id = self._multicast_vlan(multicast_path_id, tspec.src_id, dst_ids)

        result = {
            "accepted": True,
            "path_id": multicast_path_id,
            "sr_Mbps": sr_Mbps,
            "links": list(multicast_links)
        }
        if vlan_id:
            result["vlan_id"] = vlan_id
        return result

    def _route_multicast_flow(self, tspec: TSpecR, dst_ids: List[str]) -> Dict:
        """
        Route a multicast flow by sequentially selecting paths to each destination.
        Links already in the path are reused without additional admission control.

        Args:
            tspec: Flow specification with multiple destinations
            dst_ids: List of destination endpoint IDs

        Returns:
            Dict with 'accepted', 'path_id', 'sr_Mbps', and optionally 'reason'
        """
        if self.cfg.routing_policy == "largest_residual_bandwidth_minimum_tree":
            return self._route_multicast_flow_minimum_tree(tspec, dst_ids)

        # Track all links used in the multicast path
        multicast_links = set()
        # Track links by type for IIE format construction
        ingress_links = set()
        interior_links = set()
        egress_links = set()

        # Required shaping rate (will be same for all destinations)
        sr_Mbps = None

        # Ingress cover for admission control
        ingress_cover_sets = {lid: set() for lid in self.topo.links if lid not in self.topo.ingress_links}
        endpoints = [n for n, d in self.topo.nodes.items() if d.kind == 'endpoint']
        for s in endpoints:
            for t in endpoints:
                if s == t: continue
                key = (s, t)
                s_paths = self.routing_matrix.paths.get(key, [])
                for p in s_paths:
                    for l in p.links:
                        if l in self.topo.ingress_links: continue
                        ingress_cover_sets[l].add(p.links[0])
        ingress_cover = {lid: len(ingress_cover_sets[lid]) for lid in self.topo.links if lid not in self.topo.ingress_links}

        # Admission control helper
        adm = AdmissionControl(self.topo, slot_duration_s=self.cfg.slot_duration_s, mode=self.cfg.admission_mode,
                               conservative_cac_max_path_hops=self.cfg.conservative_cac_max_path_hops,
                               conservative_cac_max_link_latency_s=self.cfg.conservative_cac_max_link_latency_s,
                               bandwidth_overhead=self.cfg.bandwidth_overhead)

        # Process each destination sequentially
        for dst_id in dst_ids:
            # Get candidate paths for this destination
            key = (tspec.src_id, dst_id)
            candidates = self.routing_matrix.paths.get(key, [])

            if not candidates:
                return {"accepted": False, "reason": f"No paths available from {tspec.src_id} to {dst_id}"}

            # Try to find a feasible path for this destination
            path_found = False
            for path in candidates:
                # Identify which links in this path are NEW (not already in multicast path)
                new_links = [l for l in path.links if l not in multicast_links]

                # Check admission control only for NEW links
                # Create a temporary residual_Mbps that reflects already-committed links
                temp_residual = self.residual_Mbps.copy()

                # Compute required shaping rate for this path
                # (should be same for all destinations, but we compute it for each path candidate)
                res = adm.decide(tspec, [path], temp_residual, ingress_cover)

                if not res.get("accepted"):
                    continue  # Try next path

                # Path is feasible! Now check if new links have enough bandwidth
                required_sr = res["sr_Mbps"]
                if sr_Mbps is None:
                    sr_Mbps = required_sr
                elif sr_Mbps != required_sr:
                    # This shouldn't happen for same TSpec, but handle it
                    sr_Mbps = max(sr_Mbps, required_sr)

                # Check bandwidth only on NEW links
                feasible = True
                for l in new_links:
                    if temp_residual.get(l, 0) < sr_Mbps:
                        feasible = False
                        break

                if not feasible:
                    continue  # Try next path

                # This path works! Add its links to the multicast path
                for link in path.links:
                    multicast_links.add(link)

                    # Classify link by type for IIE format
                    if link in self.topo.ingress_links:
                        ingress_links.add(link)
                    elif link in self.topo.egress_links:
                        egress_links.add(link)
                    else:
                        interior_links.add(link)

                path_found = True
                break  # Move to next destination

            if not path_found:
                return {"accepted": False, "reason": f"No feasible path found for destination {dst_id}"}

        # All destinations routed successfully!
        # Construct IIE format path ID with alphabetically sorted links in each group
        ingress_str = '-'.join(sorted(ingress_links)) if ingress_links else ""
        interior_str = '-'.join(sorted(interior_links)) if interior_links else ""
        egress_str = '-'.join(sorted(egress_links)) if egress_links else ""

        multicast_path_id = f"{ingress_str}^{interior_str}^{egress_str}"

        vlan_id = self._multicast_vlan(multicast_path_id, tspec.src_id, dst_ids)

        result = {
            "accepted": True,
            "path_id": multicast_path_id,
            "sr_Mbps": sr_Mbps,
            "links": list(multicast_links)
        }

        # Add VLAN ID if available
        if vlan_id:
            result["vlan_id"] = vlan_id

        return result
