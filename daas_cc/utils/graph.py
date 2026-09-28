from __future__ import annotations
from typing import Dict, List, Tuple
import networkx as nx
from ..domain.models import Topology, Path

def make_path_id_iie(links: List[str], topology: Topology) -> str:
    """
    Generate a path ID using Ingress-Interior-Egress (IIE) format.

    Format: ingress_links^interior_links^egress_links

    - Ingress links: Links originating at source endpoints (always 1 for unicast)
    - Interior links: Links that don't originate at source or terminate at destination
    - Egress links: Links terminating at destination endpoints (1+ for multicast)

    Within each group, links are sorted alphabetically. This format:
    - Supports multicast (multiple egress links)
    - Makes ingress identification trivial (always first group)
    - Maintains deterministic ordering

    Args:
        links: List of link IDs in traversal order
        topology: Topology object to identify endpoint links

    Returns:
        Path ID string in IIE format (e.g., "u01l01_l01^l01_s01-s01_l02^l02_u01l02")
    """
    if not links:
        return ""

    # Classify links into ingress, interior, and egress
    ingress_links = []
    interior_links = []
    egress_links = []

    for link in links:
        if link in topology.ingress_links:
            # Link originates at a source endpoint
            ingress_links.append(link)
        elif link in topology.egress_links:
            # Link terminates at a destination endpoint
            egress_links.append(link)
        else:
            # Interior link
            interior_links.append(link)

    # Sort each group alphabetically
    ingress_sorted = sorted(ingress_links)
    interior_sorted = sorted(interior_links)
    egress_sorted = sorted(egress_links)

    # Join each group with hyphens
    ingress_str = '-'.join(ingress_sorted) if ingress_sorted else ""
    interior_str = '-'.join(interior_sorted) if interior_sorted else ""
    egress_str = '-'.join(egress_sorted) if egress_sorted else ""

    # Join groups with carets
    return f"{ingress_str}^{interior_str}^{egress_str}"

def parse_path_id_iie(path_id: str) -> Tuple[List[str], List[str], List[str]]:
    """
    Parse an IIE format path ID into its three components.

    Args:
        path_id: Path ID in IIE format

    Returns:
        Tuple of (ingress_links, interior_links, egress_links)
    """
    parts = path_id.split('^')
    if len(parts) != 3:
        # Fallback for non-IIE format
        return ([], [], [])

    ingress_links = parts[0].split('-') if parts[0] else []
    interior_links = parts[1].split('-') if parts[1] else []
    egress_links = parts[2].split('-') if parts[2] else []

    return (ingress_links, interior_links, egress_links)

def get_all_links_from_iie(path_id: str) -> List[str]:
    """Get all links from an IIE path ID in a flat list."""
    ingress, interior, egress = parse_path_id_iie(path_id)
    return ingress + interior + egress

def build_nx(topology: Topology) -> nx.DiGraph:
    """
    Generate a path ID from a list of link IDs using alphabetical ordering.

    This representation supports multicast/tree structures where traversal order
    is ambiguous, and makes path comparison easier through deterministic ordering.

    Args:
        links: List of link IDs in any order (typically traversal order)

    Returns:
        Path ID string with links in alphabetical order, joined by hyphens
    """
    return '-'.join(sorted(links))

def build_nx(topology: Topology) -> nx.DiGraph:
    G = nx.DiGraph()
    for nid, n in topology.nodes.items():
        G.add_node(nid, kind=n.kind, **n.attrs)
    for lid, l in topology.links.items():
        G.add_edge(l.src, l.dst, id=lid, capacity_Mbps=l.capacity_Mbps, latency=l.prop_latency_s)
    return G

def k_shortest_paths_by_hops(topology: Topology, src: str, dst: str, k: int = 8) -> List[Path]:
    G = build_nx(topology)
    paths = []
    try:
        for p in nx.shortest_simple_paths(G, src, dst, weight=None):
            links = []
            for u,v in zip(p, p[1:]):
                links.append(G[u][v]['id'])
            lat = sum(topology.links[l].prop_latency_s for l in links)
            # Use IIE format for path ID, keep links in traversal order
            path_id = make_path_id_iie(links, topology)
            paths.append(Path(id=path_id, links=links, total_prop_latency_s=lat, hop_count=len(links)))
            if len(paths) >= k:
                break
    except nx.NetworkXNoPath:
        pass
    return paths

def k_shortest_paths_by_latency(topology: Topology, src: str, dst: str, k: int = 8) -> List[Path]:
    G = build_nx(topology)
    paths = []
    try:
        for p in nx.shortest_simple_paths(G, src, dst, weight=lambda u,v,d: d.get('latency', 0.0)):
            links = []
            for u,v in zip(p, p[1:]):
                links.append(G[u][v]['id'])
            lat = sum(topology.links[l].prop_latency_s for l in links)
            # Use IIE format for path ID, keep links in traversal order
            path_id = make_path_id_iie(links, topology)
            paths.append(Path(id=path_id, links=links, total_prop_latency_s=lat, hop_count=len(links)))
            if len(paths) >= k:
                break
    except nx.NetworkXNoPath:
        pass
    return paths

def path_max_loaded_link_residual_fraction(path_links: List[str], link_residual_Mbps: Dict[str, int], capacity_Mbps: Dict[str, int]) -> float:
    """
    Find the maximum residual bandwidth among the minimum residual bandwidth links of all paths.

    This function evaluates path quality by finding the link with minimum residual bandwidth
    (the bottleneck) in the path, excluding the first (ingress) and last (egress) links.
    The exclusion ensures the policy focuses on load balancing within the network interior,
    not on edge link congestion.

    The path with the largest residual bandwidth at its bottleneck link is preferred.

    Args:
        path_links: List of link IDs in the path
        link_residual_Mbps: Dictionary mapping link IDs to residual bandwidth in Mbps
        capacity_Mbps: Dictionary mapping link IDs to capacity (unused in this implementation)

    Returns:
        Minimum residual bandwidth in Mbps among interior links (excluding first and last)
        Returns 0.0 if no interior links exist or path is empty
    """
    # Exclude first (ingress) and last (egress) links for interior load balancing
    if len(path_links) <= 2:
        # Path has only ingress and/or egress links, no interior links
        # Return 0 to deprioritize such paths in routing decisions
        return 0.0

    interior_links = path_links[1:-1]  # Exclude first and last links

    # Use absolute residual bandwidth (Mbps), not fractions
    # This ensures correct comparison when links have different capacities
    residual_bandwidths = [link_residual_Mbps[l] for l in interior_links]

    # Return the minimum residual bandwidth (the bottleneck) for this path
    # Paths with higher bottleneck bandwidth are preferred
    return min(residual_bandwidths) if residual_bandwidths else 0.0
