from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, TypedDict, Iterable
from .wf2q import NodeScheduler, FlowQueue
from ..utils.graph import make_path_id_iie, parse_path_id_iie, get_all_links_from_iie
from ..domain.models import Topology

@dataclass
class PSS:
    period_slots: int
    slot_duration_s: float
    bytes_per_slot: int
    slots: List[str]

@dataclass
class IngressRASPlan:
    ingress_link: str
    pss: PSS
    # df_id -> bundle_id
    bundle_id_by_flow: Dict[str, str]
    # bundle_id -> list[df_id]
    bundle_members: Dict[str, List[str]]
    # bundle_id -> path link list
    bundle_path_links: Dict[str, List[str]]
    # bundle_id -> effective scheduled rate in Mbps (= max(sr_Mbps, overhead-adjusted sum of R_Min))
    bundle_effective_rates: Dict[str, int] = field(default_factory=dict)

class FlowDict(TypedDict):
    sr_Mbps: int
    l_max_bytes: int
    path_link_ids: List[str]
    r_min_Mbps: int


# Creation of the flow bundles: all flows in a bundle have identical shaping rate, maximum packet size,
# and network path, and the sum of their packet sizes fits within the duration of a PSS timeslot.
# A path is represented as a list of link name strings.
# From a network perspective, the bundle is also identified by its ingress link.
def _pack_bundles_for_ingress(
    ingress_link: str,
    bytes_per_slot: int,
    # flow_info: df_id -> (sr_Mbps, l_max_bytes, path_link_ids)
    flow_info: Dict[str, Tuple[int, int, List[str]]],
    topology: Topology,  # ADD: For topology-based link classification
    # Return a tuple of two dictionaries:
    # First maps the bundle name to the df_ids of its flows
    # Second maps the bundle name name to its properties
) -> Tuple[Dict[str, List[str]], Dict[str, Tuple[int, int, List[str]]]]:
    """
    Greedy packer that groups flows into bundles per v0.04 rules:
      – same path_id, same sr_Mbps, same l_max_bytes
      – sum(l_max_bytes) <= bytes_per_slot
    Returns:
      bundles: bundle_id -> list of df_ids
      bundle_props: bundle_id -> (sr_Mbps, l_max_bytes, path_link_ids)
    In 'bundle_props', 'props' stands for 'properties'
    """
    # group key is (path_id, sr_Mbps, l_max_bytes)
    # derive path_id strings from link-ids list to keep stable IDs
    by_key: Dict[Tuple[str, int, int], List[str]] = {}      # Props key maps onto list of link names
    path_id_by_key: Dict[Tuple[str, int, int], str] = {}    # Props key maps onto path name derived from link names

    # Build path_id_by_key: for every set of properties, define the corresponding path name
    # With IIE format, we need to check if ingress_link is in the ingress group
    for df_id, (sr_Mbps, lmax, links) in flow_info.items():
        if not links:
            continue

        # Check if first link (in traversal order) is the ingress link we're processing
        if links[0] != ingress_link:
            continue

        # Generate IIE format path_id from traversal-order links
        # Use topology-based classification instead of position assumptions
        ingress_links_list = [l for l in links if l in topology.ingress_links]
        egress_links_list = [l for l in links if l in topology.egress_links]
        interior_links_list = [l for l in links if l not in topology.ingress_links
                              and l not in topology.egress_links]

        # Sort each group alphabetically
        ingress_str = '-'.join(sorted(ingress_links_list))
        interior_str = '-'.join(sorted(interior_links_list)) if interior_links_list else ""
        egress_str = '-'.join(sorted(egress_links_list)) if egress_links_list else ""

        path_id = f"{ingress_str}^{interior_str}^{egress_str}"

        key = (path_id, sr_Mbps, lmax)
        # by_key maps flow properties onto the respective df_ids
        by_key.setdefault(key, []).append(df_id)
        # path_id_by_key maps the flow properties onto path ids
        path_id_by_key[key] = path_id

    # DEBUG
    # print("flow_info: {}".format(flow_info))
    # print("by_key: {}".format(by_key))
    # print("path_id_by_key: {}".format(path_id_by_key))
    # END DEBUG

    # Map the bundle id onto the list of flows that it includes
    bundles: Dict[str, List[str]] = {}

    # Map the bundle id onto the tuple of bundle properties
    bundle_props: Dict[str, Tuple[int, int, List[str]]] = {}

    for key, df_ids in by_key.items():
        path_id, sr_Mbps, lmax = key
        # simple first-fit decreasing by L_Max
        df_ids_sorted = sorted(df_ids, key=lambda d: flow_info[d][1], reverse=True)
        current: List[str] = []
        used = 0
        idx = 0
        bundle_idx = 0
        while idx < len(df_ids_sorted):
            d = df_ids_sorted[idx]
            size = flow_info[d][1]
            if size > bytes_per_slot:
                # Flow frame is larger than one slot — warn and place it alone.
                # First close any open bundle.
                if current:
                    b_id = f"b::{ingress_link}::{path_id}::{sr_Mbps}::{lmax}::{bundle_idx}"
                    bundles[b_id] = current
                    bundle_props[b_id] = (sr_Mbps, lmax, flow_info[current[0]][2])
                    bundle_idx += 1
                    current = []
                    used = 0
                # Place the oversized flow in its own bundle.
                print(f"[WARN] _pack_bundles_for_ingress: flow {d} l_max_bytes={size} > "
                      f"bytes_per_slot={bytes_per_slot}; placing in single-flow bundle")
                b_id = f"b::{ingress_link}::{path_id}::{sr_Mbps}::{lmax}::{bundle_idx}"
                bundles[b_id] = [d]
                bundle_props[b_id] = (sr_Mbps, lmax, flow_info[d][2])
                bundle_idx += 1
                idx += 1
            elif used + size <= bytes_per_slot:
                current.append(d)
                used += size
                idx += 1
            else:
                # Current bundle is full — close it and start a new one.
                b_id = f"b::{ingress_link}::{path_id}::{sr_Mbps}::{lmax}::{bundle_idx}"
                bundles[b_id] = current
                bundle_props[b_id] = (sr_Mbps, lmax, flow_info[current[0]][2])
                bundle_idx += 1
                current = []
                used = 0
                # Do NOT advance idx — retry this flow in the new bundle.
        if current:
            b_id = f"b::{ingress_link}::{path_id}::{sr_Mbps}::{lmax}::{bundle_idx}"
            bundles[b_id] = current
            bundle_props[b_id] = (sr_Mbps, lmax, flow_info[current[0]][2])

    return bundles, bundle_props

def build_ingress_pss(
    ingress_link: str,
    link_capacity_Mbps: int,
    scheduling_policy: str,
    slot_duration_s: float,
    period_slots: int,
    # df_id -> dict(sr_Mbps, l_max_bytes, path_link_ids)
    flow_info: FlowDict,
    # link_id -> link_virtual_rates, as needed by the auxiliary scheduler
    link_virtual_rates: Dict[str, int],
    topology: Topology,  # ADD: For topology-based link classification
    bandwidth_overhead: float = 0.0,
) -> IngressRASPlan:
    """
    v0.04 PSS builder:
      – Top-level MD-SCFQ over paths (child rate = sum of bundle rates on that path + virtual empty queue).
      – Bottom-level MD-SCFQ over bundles for the eligible path.
      – A path is eligible only if all its links' link_work values are eligible (auxiliary link shaping).
      – Within each timeslot, we assign the chosen bundle ID to the slot.
    """
    # PRECISION for floating-point comparisons
    EPSILON = 1e-9

    # Bytes per PSS slot
    bytes_per_slot = int(1e06 * link_capacity_Mbps * slot_duration_s) // 8
    # normalize incoming flow_info; map each flow id with the shaping rate, max packet size, and path
    flow_info_norm: Dict[str, Tuple[int, int, List[str]]] = {}

    # DEBUG
    # print("flow_info: {}".format(flow_info))
    # END DEBUG

    for df_id, d in flow_info.items():
        flow_info_norm[df_id] = (int(d["sr_Mbps"]), int(d["l_max_bytes"]), list(d["path_link_ids"]))

    # DEBUG
    # print("flow_info_norm: {}".format(flow_info_norm))
    # END DEBUG

    # Create the bundles for the ingress_link.
    # The bundle ID is a string with the following fields:
    # "b::{ingress_link}::{path_id}::{sr_Mbps}::{lmax}::{bundle_idx}"
    # Bundles with the same properties differ by the value of bundle_idx
    bundles, bundle_props = _pack_bundles_for_ingress(
        ingress_link=ingress_link, bytes_per_slot=bytes_per_slot, flow_info=flow_info_norm, topology=topology
    )

    # DEBUG
    # 'bundles' maps b_ids onto lists of df_ids
    # print("bundles: {}".format(bundles))
    # bundle_props maps b_ids onto bundle properties
    # print("bundle_props: {}".format(bundle_props))
    # END DEBUG

    # Effective bundle service rate: max(sr_Mbps, floor((100 + overhead) * sum(r_min_i) / 100))
    # Ensures the bundle scheduling weight covers the aggregate minimum throughput of its member flows.
    bundle_effective_rate: Dict[str, int] = {}
    for b_id, df_ids in bundles.items():
        b_sr_Mbps = bundle_props[b_id][0]
        n = len(df_ids)
        sum_r_min = sum(int(flow_info[df_id].get("r_min_Mbps", 0)) for df_id in df_ids)
        min_rate = int((100.0 + bandwidth_overhead) * sum_r_min / 100.0)
        effective = max(b_sr_Mbps, min_rate)
        bundle_effective_rate[b_id] = effective
        if n > 1 or min_rate > b_sr_Mbps:
            print(f"[PSS] bundle {b_id}: n={n}, sr_Mbps={b_sr_Mbps}, "
                  f"sum_r_min={sum_r_min}, overhead={bandwidth_overhead}%, "
                  f"min_rate={min_rate}, effective_rate={effective} Mbps")

    # Build per-path bundle lists
    bundles_by_path: Dict[str, List[str]] = {}

    # Discriminate between flat and non-flat shapers
    if scheduling_policy == "flat":
        bundle_counter = 0
        for b_id, (_, _lmax, links) in bundle_props.items():
            bundle_counter += 1
            path_id = "p-" + str(bundle_counter)
            bundles_by_path.setdefault(path_id, []).append(b_id)
    else:
        # "ras" or "plain_hierarchical"
        for b_id, (_, _lmax, links) in bundle_props.items():
            # Generate IIE format path_id from traversal-order links
            # Use topology-based classification instead of position assumptions
            ingress_links_list = [l for l in links if l in topology.ingress_links]
            egress_links_list = [l for l in links if l in topology.egress_links]
            interior_links_list = [l for l in links if l not in topology.ingress_links
                                  and l not in topology.egress_links]

            # Sort each group alphabetically
            ingress_str = '-'.join(sorted(ingress_links_list))
            interior_str = '-'.join(sorted(interior_links_list)) if interior_links_list else ""
            egress_str = '-'.join(sorted(egress_links_list)) if egress_links_list else ""

            path_id = f"{ingress_str}^{interior_str}^{egress_str}"
            bundles_by_path.setdefault(path_id, []).append(b_id)

    # DEBUG
    # bundles_by_path maps paths onto the set of bundles that use them
    # print("bundles_by_path: {}".format(bundles_by_path))
    # END DEBUG

    # Schedulers
    # Top-level over paths (+ virtual empty queue as specified)
    top = NodeScheduler(id=f"top::{ingress_link}", T=1.0)
    # bottom-level scheduler per path id
    bottom_by_path: Dict[str, NodeScheduler] = {}
    # path aggregate rate is sum of bundle rates on that path
    for path_id, b_ids in bundles_by_path.items():
        agg_Mbps = sum(bundle_effective_rate[b] for b in b_ids)
        top.children[path_id] = FlowQueue(id=path_id, rate_Mbps=agg_Mbps,
                                          T=link_capacity_Mbps/agg_Mbps,
                                          F=link_capacity_Mbps/agg_Mbps)
        ns = NodeScheduler(id=f"bottom::{path_id}",T=link_capacity_Mbps/agg_Mbps)
        for b in b_ids:
            ns.children[b] = FlowQueue(id=b, rate_Mbps=bundle_effective_rate[b],
                                       T=link_capacity_Mbps/bundle_effective_rate[b],
                                       F=link_capacity_Mbps/bundle_effective_rate[b])
        bottom_by_path[path_id] = ns
    # virtual empty queue: capacity not reserved by deterministic bundles
    used_Mbps = sum(f.rate_Mbps for f in top.children.values())
    residual_Mbps = max(link_capacity_Mbps - used_Mbps, 0)
    # Add the empty queue to the top scheduling node and initialize it.
    # If residual_Mbps == 0 (link fully loaded), the empty queue is assigned
    # an infinite service interval so it is never chosen.
    if residual_Mbps > 0:
        empty_T = link_capacity_Mbps / residual_Mbps
    else:
        empty_T = float('inf')
    top.children["empty"] = FlowQueue(id="empty", rate_Mbps=residual_Mbps,
                                      T=empty_T,
                                      F=empty_T)

    # DEBUG
    #print("used_Mbps: {}".format(used_Mbps))
    #print("residual_Mbps: {}".format(residual_Mbps))
    #print("top.children: {}".format(top.children))
    #print("bottom_by_path: {}".format(bottom_by_path))
    # END DEBUG

    # Auxiliary link shaping: initialize service intervals and link_work values
    min_spacing: Dict[str, float] = dict(link_virtual_rates)  # immutable baseline per link
    for l in min_spacing.keys():
        min_spacing[l] = link_capacity_Mbps / link_virtual_rates[l]
    # All links are eligible at the beginning
    link_work: Dict[str, float] = {l: 0.0 for l in min_spacing.keys()}

    # DEBUG
    # print("min_spacing: {}".format(min_spacing))
    # print("link_work: {}".format(link_work))
    # END DEBUG

    # DEBUG
    # link_work maintains the service eligibility for the links
    # print("link_work: {}".format(link_work))
    # min_spacing is the service interval T for each link
    # print("min_spacing: {}".format(min_spacing))
    # END DEBUG

    # List of assigned timeslots
    assignment: List[str] = []

    # Counters for debug/telemetry (ras only)
    assignments_bundle0 = 0
    eligibility_histogram: Dict[str, int] = {
        "0": 0,
        "(0,10]": 0,
        "(10,20]": 0,
        "(20,30]": 0,
        "(30,40]": 0,
        "(40,50]": 0,
        "(50,60]": 0,
        "(60,70]": 0,
        "(70,80]": 0,
        "(80,90]": 0,
        "(90,100)": 0,
        "100": 0,
    }

    def _histogram_bin(eligible_links: int, total_links: int) -> str:
        """Map eligible/total link counts to the requested percentage bins."""
        if total_links <= 0:
            # No links (shouldn't happen for a real path) -> treat as 0
            return "0"
        if eligible_links <= 0:
            return "0"
        if eligible_links >= total_links:
            return "100"

        pct = (eligible_links / total_links) * 100.0
        # Exact endpoints already handled above.
        if 0.0 < pct <= 10.0:
            return "(0,10]"
        if 10.0 < pct <= 20.0:
            return "(10,20]"
        if 20.0 < pct <= 30.0:
            return "(20,30]"
        if 30.0 < pct <= 40.0:
            return "(30,40]"
        if 40.0 < pct <= 50.0:
            return "(40,50]"
        if 50.0 < pct <= 60.0:
            return "(50,60]"
        if 60.0 < pct <= 70.0:
            return "(60,70]"
        if 70.0 < pct <= 80.0:
            return "(70,80]"
        if 80.0 < pct <= 90.0:
            return "(80,90]"
        # Remaining strict interior of (90,100)
        return "(90,100)"

    # Fill the timeslots one by one
    for ii in range(period_slots):

        # eligible paths must have all links eligible for service
        def path_is_eligible(pid: str) -> bool:
            if pid == "empty":
                return True
            # For flat policy, all fictitious paths are always link-eligible
            # (no real link topology to check)
            if scheduling_policy == "flat":
                return True
            # For ras/plain_hierarchical: check actual link eligibility
            # Extract links from IIE format path ID
            links = get_all_links_from_iie(pid)
            # A link is eligible if link_work[l] <= current slot
            # Use epsilon tolerance to handle floating-point precision errors
            return all(0.0 <= link_work.get(l, -1.0) <= ii + EPSILON for l in links)

        def eligible_link_counts(pid: str) -> tuple[int, int]:
            """Return (eligible_links, total_links) for a path at time ii."""
            if pid in (None, "empty"):
                return (0, 0)
            if scheduling_policy == "flat":
                return (0, 0)
            links = get_all_links_from_iie(pid)
            if not links:
                return (0, 0)
            eligible = 0
            for l in links:
                if 0.0 <= link_work.get(l, -1.0) <= ii + EPSILON:
                    eligible += 1
            return (eligible, len(links))

        # Determine which paths are eligible based on virtual time (S <= V)
        time_eligible_paths = [fq for fq in top.children.values() if fq.S <= top.V + EPSILON]

        # DEBUG: Print detailed info around slot 15000

        #if 14995 <= ii <= 15005:
        #    print(f"[SLOT {ii}] top.V={top.V:.6f} top.T={top.T:.6f}")
        #    for path_name, fq in top.children.items():
        #        eligible = "YES" if fq.S <= top.V + EPSILON else "NO"
        #        print(f"  {path_name}: S={fq.S:.6f} F={fq.F:.6f} T={fq.T:.6f} rate={fq.rate_Mbps} eligible={eligible}")
        # END DEBUG

        if not time_eligible_paths:
            # No paths are time-eligible: select path with minimum F to advance virtual time
            # This prevents virtual time from freezing
            chosen = min(top.children.values(), key=lambda fq: fq.F)
            chosen.S = chosen.F
            chosen.F = chosen.S + chosen.T
            top.V += top.T
            assignment.append("empty")
            if scheduling_policy == "ras":
                assignments_bundle0 += 1
            continue

        chosen_path_id = None
        selection_kind: Optional[str] = None  # 'full' | 'partial' | 'none'

        if scheduling_policy == "ras":
            # RAS policy: among time-eligible paths, prefer those with all links eligible.
            # If none are fully link-eligible, fall back to the earliest finish time.
            link_eligible_paths = [fq for fq in time_eligible_paths if fq.id == "empty" or path_is_eligible(fq.id)]
            chosen = min(link_eligible_paths or time_eligible_paths, key=lambda fq: fq.F)
            chosen_path_id = chosen.id
            chosen.S = chosen.F
            chosen.F = chosen.S + chosen.T
            top.V += top.T
        else:
            # plain_hierarchical or flat: select among time-eligible paths (minimum F)
            # Optimized: directly find minimum F
            chosen = min(time_eligible_paths, key=lambda fq: fq.F)
            chosen_path_id = chosen.id
            chosen.S = chosen.F
            chosen.F = chosen.S + chosen.T
            top.V += top.T

        # DEBUG
        # print("slot {} chosen_path_id: {}".format(ii, chosen_path_id))
        # END DEBUG

        if chosen_path_id in (None, "empty"):
            assignment.append("empty")
            if scheduling_policy == "ras":
                assignments_bundle0 += 1
            continue

        # Bottom-level selection among bundles on chosen path
        bottom = bottom_by_path.get(chosen_path_id)

        if not bottom or not bottom.children:
            assignment.append("empty")
            if scheduling_policy == "ras":
                assignments_bundle0 += 1
            continue
        chosen_bundle_id = bottom.schedule_one_slot()
        if chosen_bundle_id is None:
            assignment.append("empty")
            if scheduling_policy == "ras":
                assignments_bundle0 += 1
            continue

        assignment.append(chosen_bundle_id)

        # DEBUG
        #print("slot {} path: {} top.V: {} pathS: {} pathF: {} bundle: {} bundleS: {} bundleF: {}".format(ii, chosen_path_id, top.V, chosen.S, chosen.F, chosen_bundle_id, bottom.children[chosen_bundle_id].S, bottom.children[chosen_bundle_id].F))
        # END DEBUG


        if scheduling_policy == "ras":
            elig, tot = eligible_link_counts(chosen_path_id)
            b = _histogram_bin(elig, tot)
            eligibility_histogram[b] = eligibility_histogram.get(b, 0) + 1

        # update link_work for all links of the chosen path
        # This must happen for RAS policy regardless of whether selection was via
        # link eligibility or fallback to plain_hierarchical
        if scheduling_policy == "ras" and chosen_path_id != "empty":
            # Extract links from IIE format path ID
            path_links = get_all_links_from_iie(chosen_path_id)
            for l in path_links:
                if l in min_spacing:
                    link_work[l] = link_work.get(l, 0.0) + min_spacing[l]

        # DEBUG
        # print("slot {} bundle_id {}  bundle_rate {} link_work {}".format(ii, chosen_bundle_id, bundle_props[chosen_bundle_id][0], link_work))
        # END DEBUG

    # Build mappings to return
    bundle_id_by_flow: Dict[str, str] = {}
    bundle_members: Dict[str, List[str]] = {}
    bundle_path_links: Dict[str, List[str]] = {}
    for b_id, members in bundles.items():
        bundle_members[b_id] = list(members)
        for df in members:
            bundle_id_by_flow[df] = b_id
        bundle_path_links[b_id] = bundle_props[b_id][2]

    if scheduling_policy == "ras":
        # Print in deterministic bin order
        bins_in_order = [
            "0",
            "(0,10]",
            "(10,20]",
            "(20,30]",
            "(30,40]",
            "(40,50]",
            "(50,60]",
            "(60,70]",
            "(70,80]",
            "(80,90]",
            "(90,100)",
            "100",
        ]
        hist_str = " ".join(f"{k}={eligibility_histogram.get(k, 0)}" for k in bins_in_order)
        print(
            f"[PSS] ingress={ingress_link} policy={scheduling_policy} slots={period_slots} "
            f"bundle0(empty)={assignments_bundle0} eligibility%: {hist_str}"
        )

    return IngressRASPlan(
        ingress_link=ingress_link,
        pss=PSS(period_slots=period_slots, slot_duration_s=slot_duration_s, bytes_per_slot=bytes_per_slot, slots=assignment),
        bundle_id_by_flow=bundle_id_by_flow,
        bundle_members=bundle_members,
        bundle_path_links=bundle_path_links,
        bundle_effective_rates=bundle_effective_rate,
    )
