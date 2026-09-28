"""
Map a multicast path in IIE form to the VLAN id configured for that tree.

The map is the multicast VLAN file named by the controller configuration.
Unicast VLAN ids stay in the topology paths table.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional, Set
import logging

logger = logging.getLogger(__name__)


class MulticastVLANMapper:
    """
    Map multicast paths to VLAN ids.

    The mapper loads the multicast VLAN file and looks up a flow's
    ingress^interior^egress path.
    """

    def __init__(self, vlan_mapping_file: str = None, topology=None):
        """
        Initialize the multicast VLAN mapper.

        Args:
            vlan_mapping_file: Path to the VLAN mapping JSON file.
                             If None, looks for default file in examples directory.
            topology: Topology object to derive endpoint-to-leaf mapping (optional).
        """
        self.vlan_mapping_file = vlan_mapping_file
        self.vlan_to_path: Dict[str, Dict] = {}  # VLAN ID -> path info
        self.path_to_vlan: Dict[str, str] = {}   # path_key -> VLAN ID
        self.destination_index: Dict[str, Dict[frozenset, List[str]]] = {}  # source -> destinations -> VLANs
        self.endpoint_to_leaf: Dict[str, str] = {}  # endpoint -> leaf switch (derived from topology)
        self.topology = topology

        # Build endpoint-to-leaf mapping from topology if provided
        if topology:
            self._build_endpoint_mapping(topology)

        if vlan_mapping_file:
            self.load_mapping(vlan_mapping_file)

    def _build_endpoint_mapping(self, topology):
        """
        Build endpoint-to-leaf mapping from topology.

        For each endpoint, find which leaf switch it connects to by examining
        the ingress links (links from endpoint to switch).
        """
        for link_id, link in topology.links.items():
            # Ingress links go from endpoint to switch
            if link_id in topology.ingress_links:
                src_node = link.src  # This is the endpoint
                dst_node = link.dst  # This is the switch

                # Verify source is an endpoint and destination is a switch
                if src_node in topology.nodes and topology.nodes[src_node].kind == 'endpoint':
                    if dst_node in topology.nodes and topology.nodes[dst_node].kind == 'switch':
                        self.endpoint_to_leaf[src_node] = dst_node

        logger.info(f"Built endpoint-to-leaf mapping for {len(self.endpoint_to_leaf)} endpoints")
        logger.debug(f"Endpoint mapping: {self.endpoint_to_leaf}")

    def load_mapping(self, mapping_file: str):
        """
        Load VLAN mapping from JSON file.

        Args:
            mapping_file: Path to the VLAN mapping JSON file
        """
        mapping_path = Path(mapping_file)

        if not mapping_path.exists():
            logger.warning(f"VLAN mapping file not found: {mapping_file}")
            return

        try:
            with open(mapping_path, 'r') as f:
                self.vlan_to_path = json.load(f)

            logger.info(f"Loaded {len(self.vlan_to_path)} VLAN mappings from {mapping_file}")

            # Build reverse index: path characteristics -> VLAN
            self._build_indices()

        except Exception as e:
            logger.error(f"Failed to load VLAN mapping: {e}")
            raise

    def _build_indices(self):
        """Build fast lookup indices from the VLAN mapping."""
        # Build path-based index for VLAN lookup
        # Key: normalized IIE path -> Value: VLAN ID
        # Since each VLAN can have multiple IIE path variants (one per ingress link),
        # we index ALL variants to the same VLAN ID

        for vlan_id, path_info in self.vlan_to_path.items():
            network_paths = path_info.get('network_paths', [])

            # Handle both list and single string (backward compatibility)
            if isinstance(network_paths, str):
                network_paths = [network_paths] if network_paths else []

            if not network_paths:
                logger.warning(f"VLAN {vlan_id} has no network_paths")
                continue

            # Index each IIE path variant to this VLAN
            for network_path in network_paths:
                # Normalize the network path to match flow IIE format
                normalized_path = self._normalize_network_path(network_path)
                if normalized_path:
                    if normalized_path in self.path_to_vlan:
                        logger.debug(f"Path collision: {normalized_path} already mapped to {self.path_to_vlan[normalized_path]}, also maps to {vlan_id}")
                    self.path_to_vlan[normalized_path] = vlan_id
                    logger.debug(f"Indexed VLAN {vlan_id}: {normalized_path}")

        logger.info(f"Built path index: {len(self.path_to_vlan)} unique path variants mapping to {len(self.vlan_to_path)} VLANs")

    def _normalize_network_path(self, network_path: str) -> str:
        """
        Normalize network_path from VLAN mapping to match flow IIE format.

        The network_path may have format like:
        "from_lf01_endpoints^l01_s01-l01_s02^s01_l02-s01_l03^l04_u01l04^l03_u02l03"

        Need to convert to standard IIE format:
        "ingress_links^interior_links^egress_links"

        Args:
            network_path: Network path from VLAN mapping

        Returns:
            Normalized path in IIE format, or empty string if cannot normalize
        """
        try:
            parts = network_path.split('^')
            if len(parts) < 3:
                return ""

            # Skip placeholder ingress if present
            start_idx = 0
            if parts[0].startswith('from_'):
                start_idx = 1

            # Collect all link parts (skip the placeholder)
            link_parts = [p for p in parts[start_idx:] if p and not p.startswith('from_')]

            if len(link_parts) < 2:
                return ""

            # First part(s) could be ingress, middle is interior, last part(s) is egress
            # For now, use simple heuristic: last parts with '_u' are egress
            ingress = []
            interior = []
            egress = []

            for part in link_parts:
                # Links with '_u' pattern (leaf_endpoint) are egress
                if '_u' in part and any(ep in part for ep in ['u01', 'u02', 'u03', 'u04']):
                    egress.extend(part.split('-'))
                # Links with 's' (spine) are interior
                elif '_s' in part or part.startswith('s'):
                    interior.extend(part.split('-'))
                # Others are ingress
                else:
                    ingress.extend(part.split('-'))

            # Build normalized IIE path
            ingress_str = '-'.join(sorted(ingress)) if ingress else ""
            interior_str = '-'.join(sorted(interior)) if interior else ""
            egress_str = '-'.join(sorted(egress)) if egress else ""

            return f"{ingress_str}^{interior_str}^{egress_str}"

        except Exception as e:
            logger.error(f"Error normalizing network path {network_path}: {e}")
            return ""

    def _make_path_key(self, source: str, destinations: Set[str]) -> str:
        """
        Create a canonical key for a multicast path.

        Args:
            source: Source endpoint ID
            destinations: Set of destination endpoint IDs

        Returns:
            Canonical path key string
        """
        # Sort destinations for canonical representation
        dest_str = '-'.join(sorted(destinations))
        return f"{source}→{dest_str}"

    def get_vlan_for_multicast(self, source: str = None, destinations: List[str] = None, iie_path: str = None) -> Optional[str]:
        """
        Get VLAN ID for a multicast flow based on its IIE path.

        Args:
            source: Source endpoint ID (optional, for logging)
            destinations: List of destination endpoint IDs (optional, for logging)
            iie_path: IIE format path (ingress^interior^egress) - REQUIRED for path-based matching

        Returns:
            VLAN ID as hex string (e.g., "0x100") or None if not found
        """
        if not iie_path:
            logger.warning("No IIE path provided for VLAN lookup")
            return None

        # Normalize the flow's IIE path for matching
        normalized_flow_path = self._normalize_flow_iie_path(iie_path)

        if not normalized_flow_path:
            logger.warning("Could not normalize flow IIE path: %s", iie_path)
            return None

        # Direct path lookup
        vlan_id = self.path_to_vlan.get(normalized_flow_path)

        if vlan_id:
            logger.debug("Found VLAN %s for path: %s", vlan_id, normalized_flow_path)
            return vlan_id

        logger.debug("No VLAN found for IIE path: %s", iie_path)
        logger.debug("Normalized flow path: %s", normalized_flow_path)
        logger.debug("Available normalized paths: %s", len(self.path_to_vlan))

        # Show some similar paths for debugging
        if self.path_to_vlan:
            sample_paths = list(self.path_to_vlan.keys())[:3]
            logger.debug(f"Sample indexed paths: {sample_paths}")

        return None

    def _normalize_flow_iie_path(self, iie_path: str) -> str:
        """
        Normalize a flow's IIE path for matching against VLAN mapping.

        SIMPLIFIED APPROACH: Split by ^ and sort links in each stage.
        This matches the simplified normalization for stored paths.

        Flow IIE paths are in format: "ingress_links^interior_links^egress_links"
        Each group has links separated by dashes.

        Args:
            iie_path: IIE path from flow

        Returns:
            Normalized path for matching
        """
        try:
            parts = iie_path.split('^')

            # IIE paths should have exactly 3 components
            if len(parts) != 3:
                logger.warning(f"IIE path doesn't have 3 components: {iie_path} (has {len(parts)})")
                # Try to handle gracefully
                while len(parts) < 3:
                    parts.append("")

            # Normalize each stage: sort links (separated by -)
            normalized_parts = []
            for part in parts[:3]:  # Only process first 3 components
                if not part or part.strip() == "":
                    normalized_parts.append("")
                else:
                    # Split links by dash, sort, and rejoin
                    links = part.split('-')
                    sorted_links = sorted([link.strip() for link in links if link.strip()])
                    normalized_parts.append('-'.join(sorted_links))

            return f"{normalized_parts[0]}^{normalized_parts[1]}^{normalized_parts[2]}"

        except Exception as e:
            logger.error(f"Error normalizing flow IIE path {iie_path}: {e}")
            return ""

    def get_vlan_info(self, vlan_id: str) -> Optional[Dict]:
        """
        Get complete information for a VLAN ID.

        Args:
            vlan_id: VLAN ID as hex string (e.g., "0x100")

        Returns:
            Dictionary with path info (mgid, ports, destinations, source) or None
        """
        return self.vlan_to_path.get(vlan_id)

    def get_mgid(self, vlan_id: str) -> Optional[int]:
        """
        Get MGID for a VLAN ID.

        Args:
            vlan_id: VLAN ID as hex string

        Returns:
            MGID integer or None
        """
        info = self.get_vlan_info(vlan_id)
        return info.get('mgid') if info else None

    def get_all_vlans_for_source(self, source: str) -> List[str]:
        """
        Get all VLAN IDs originating from a source.

        Args:
            source: Source endpoint ID

        Returns:
            List of VLAN IDs
        """
        vlans = []
        for vlan_id, info in self.vlan_to_path.items():
            if info.get('source') == source:
                vlans.append(vlan_id)
        return vlans

    def get_statistics(self) -> Dict:
        """
        Get statistics about the VLAN mapping.

        Returns:
            Dictionary with statistics
        """
        sources = set()
        destinations = set()
        mgids = set()

        for info in self.vlan_to_path.values():
            if info.get('source'):
                sources.add(info['source'])
            if info.get('destinations'):
                destinations.update(info['destinations'])
            if info.get('mgid'):
                mgids.add(info['mgid'])

        return {
            'total_vlans': len(self.vlan_to_path),
            'unique_sources': len(sources),
            'unique_destinations': len(destinations),
            'unique_mgids': len(mgids),
            'sources': sorted(sources),
            'destinations': sorted(destinations),
            'mgid_range': f"{min(mgids)}-{max(mgids)}" if mgids else "N/A"
        }

    def is_loaded(self) -> bool:
        """Check if mapping is loaded."""
        return len(self.vlan_to_path) > 0


def get_default_vlan_mapper(config_dir: str = None, topology=None) -> MulticastVLANMapper:
    """
    Get a VLAN mapper with default configuration.

    Args:
        config_dir: Configuration directory. If None, uses examples directory.
        topology: Topology object for deriving endpoint-to-leaf mapping.

    Returns:
        Configured MulticastVLANMapper instance
    """
    if config_dir is None:
        # Default to examples directory
        config_dir = Path(__file__).parent.parent / "examples"
    else:
        config_dir = Path(config_dir)

    mapping_file = config_dir / "multicast_vlan_mapping.json"

    mapper = MulticastVLANMapper(topology=topology)

    if mapping_file.exists():
        mapper.load_mapping(str(mapping_file))
    else:
        logger.warning(f"Default VLAN mapping file not found: {mapping_file}")

    return mapper

