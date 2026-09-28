from __future__ import annotations
import os, json
from dataclasses import fields
from typing import List
from ..domain.models import Topology, Node, Link, Flow

_NODE_FIELDS = {item.name for item in fields(Node)}

class JsonRepo:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    def save_topology(self, topo: Topology):
        path = os.path.join(self.state_dir, "topology.json")
        data = {
            "nodes": [vars(n) for n in topo.nodes.values()],
            "links": [vars(l) for l in topo.links.values()],
            "ingress_links": topo.ingress_links,
        }
        json.dump(data, open(path, "w"), indent=2)

    def load_topology(self, path: str) -> Topology:
        with open(path, "r") as f:
            data = json.load(f)
        topo = Topology()
        for n in data["nodes"]:
            topo.add_node(Node(**{k: v for k, v in n.items() if k in _NODE_FIELDS}))
        for l in data["links"]:
            topo.add_link(Link(**l))
        topo.ingress_links = data.get("ingress_links", [])

        # Populate egress_links: links that terminate at endpoint nodes
        endpoint_nodes = {nid for nid, node in topo.nodes.items() if node.kind == 'endpoint'}
        topo.egress_links = [lid for lid, link in topo.links.items() if link.dst in endpoint_nodes]

        # Load path-to-vlan_id mappings
        if "paths" in data:
            for path_entry in data["paths"]:
                path_id = path_entry.get("id")
                vlan_id = path_entry.get("vlan_id", "0x0")
                if path_id:
                    topo.paths[path_id] = vlan_id

        return topo

    def save_flows(self, flows: List[Flow]):
        path = os.path.join(self.state_dir, "flows.json")
        json.dump([f.model_dump() for f in flows], open(path, "w"), indent=2)

    def load_flows(self) -> List[Flow]:
        path = os.path.join(self.state_dir, "flows.json")
        if not os.path.exists(path):
            return []
        return [Flow(**d) for d in json.load(open(path))]
