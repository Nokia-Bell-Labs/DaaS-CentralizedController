#
#                © 2026 Nokia
#                Licensed under the BSD 3-Clause Clear License
#                SPDX-License-Identifier: BSD-3-Clause-Clear
#
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field, ConfigDict

@dataclass
class Node:
    id: str     # Node name
    kind: str   # 'switch' or 'endpoint'
    ip_address: str = ""  # data-plane address used in STREAMS.cfg
    attrs: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Link:
    id: str
    src: str    # node id (not ip_address)
    dst: str    # node if (no ip_address)
    capacity_Mbps: int  # C_e
    prop_latency_s: float  # lambda_e (seconds)

@dataclass
class Path:
    id: str
    links: List[str]
    total_prop_latency_s: float
    hop_count: int

    def __repr__(self) -> str:
        return f"Path(id={self.id}, hops={self.hop_count}, latency={self.total_prop_latency_s*1e6:.1f}us)"

@dataclass
class Topology:
    nodes: Dict[str, Node] = field(default_factory=dict)
    links: Dict[str, Link] = field(default_factory=dict)
    ingress_links: List[str] = field(default_factory=list)
    egress_links: List[str] = field(default_factory=list)  # Links terminating at endpoints
    paths: Dict[str, str] = field(default_factory=dict)  # path_id -> vlan_id mapping
    out_links_by_node: Dict[str, List[str]] = field(default_factory=dict)
    in_links_by_node: Dict[str, List[str]] = field(default_factory=dict)

    def add_node(self, node: Node):
        self.nodes[node.id] = node
        self.out_links_by_node.setdefault(node.id, [])
        self.in_links_by_node.setdefault(node.id, [])

    def add_link(self, link: Link):
        self.links[link.id] = link
        self.out_links_by_node.setdefault(link.src, []).append(link.id)
        self.in_links_by_node.setdefault(link.dst, []).append(link.id)

    def path_latency(self, path: Path) -> float:
        return sum(self.links[l].prop_latency_s for l in path.links)

    def links_of_path(self, path_id: str):
        """
        Extract link IDs from a path ID.

        Supports IIE format (ingress^interior^egress) and legacy format (link-link-link).
        """
        if '^' in path_id:
            # IIE format: split by carets then by hyphens
            parts = path_id.split('^')
            links = []
            for part in parts:
                if part:
                    links.extend(part.split('-'))
        else:
            # Legacy format: split by hyphens
            links = path_id.split('-')

        return [l for l in links if l in self.links]

class TSpecR(BaseModel):
    model_config = ConfigDict(
        extra='allow',  # Allow extra fields (including unit_cost when added)
        json_schema_extra={
            "example": {
                "src_id": "u01l01",
                "dst_id": ["u01l02"],  # Always a list, even for unicast
                "PCS_R": {
                    "vlanPrio": "7",
                    "dscp": "0",
                    "srcPort": "0",
                    "dstPort": "0"
                },
                "D_Max_ns": 16384000,
                "L_Max_bytes": 1542,
                "B_Ref_bytes": 1600,
                "R_Min_Mbps": 15
                # Note: unit_cost is NOT specified by user - it's computed by the controller
                # based on reference_unit_cost and cost_policy
            }
        }
    )

    DF_ID: Optional[str] = None
    src_id: str
    dst_id: List[str]  # Always a list: ["u01l02"] for unicast, ["u01l02", "u01l03"] for multicast
    PCS_R: Dict[str, str] = Field(
        default_factory=lambda: {
            "vlanPrio": "7",
            "dscp": "0",
            "srcPort": "0",
            "dstPort": "0"
        },
        examples=[{
            "vlanPrio": "7",
            "dscp": "0",
            "srcPort": "0",
            "dstPort": "0"
        }]
    )
    D_Max_ns: int
    L_Max_bytes: int
    B_Ref_bytes: int
    R_Min_Mbps: int
    unit_cost: float = Field(
        default=0.0,
        description="Unit cost (USD/s) - typically not set by user; derived from reference_unit_cost and cost_policy",
        json_schema_extra={"readOnly": True}
    )

    def get_dst_ids(self) -> List[str]:
        """Get destination IDs as a list (always a list now)."""
        return self.dst_id

    def is_multicast(self) -> bool:
        """Check if this is a multicast flow (multiple destinations)."""
        return len(self.dst_id) > 1


class TSpecC(BaseModel):
    DF_ID: str
    NPL: str
    PCS_C: Dict[str, str]
    SR_Mbps: int
    MBS_bytes: int

class Flow(BaseModel):
    df_id: str
    src_id: str
    dst_id: List[str]  # Always a list: ["u01l02"] for unicast, ["u01l02", "u01l03"] for multicast
    pcs: Dict[str, str]
    path_id: str
    sr_Mbps: int
    l_max_bytes: int
    b_ref_bytes: int
    d_max_ns: int
    unit_cost: float = 0.0  # USD per second
    running_cost: float = 0.0  # USD
    time_last_update: Optional[float] = None  # seconds since epoch
    r_min_Mbps: int = 0  # Minimum guaranteed throughput requested by the flow (from TSpecR.R_Min_Mbps)
    vlan_id: Optional[str] = None  # VLAN ID for multicast flows (hex string, e.g., "0x100")

    def get_dst_ids(self) -> List[str]:
        """Get destination IDs as a list (always a list now)."""
        return self.dst_id

    def is_multicast(self) -> bool:
        """Check if this is a multicast flow (multiple destinations)."""
        return len(self.dst_id) > 1

@dataclass
class Bundle:
    """
    Flow-bundle used by the v0.04 PSS:
      – All flows in a bundle share the same path, same shaping rate, and same L_Max.
      – The sum of L_Max across member flows fits within one timeslot byte size.
    """
    id: str
    path_id: str
    sr_Mbps: int
    l_max_bytes: int
    flow_ids: List[str] = field(default_factory=list)
