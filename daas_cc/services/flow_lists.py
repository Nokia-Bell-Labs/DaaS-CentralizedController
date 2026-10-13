#
#                © 2026 Nokia
#                Licensed under the BSD 3-Clause Clear License
#                SPDX-License-Identifier: BSD-3-Clause-Clear
#
"""Load hand-written flow lists into the dictionaries the controller already admits.

.ufl files describe unrouted requests (the controller chooses the path).
.rfl files describe routed requests (the file names the interior links).
.json files are accepted as the records those two formats expand to.
The controller's flow-table log is itself a .rfl file.

The Class and Type columns are accepted and not stored. Every request uses the
same packet classification as the previous .ufl converter: VLAN priority 7 and
DSCP 46. Latency in the file is microseconds and is stored as nanoseconds.
Payload bytes gain 70 bytes of framing to become L_Max.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..domain.models import Topology

logger = logging.getLogger(__name__)

_PCS_R = {
    "vlanPrio": "7",
    "dscp": "46",
    "srcPort": "0",
    "dstPort": "0",
}


def load_unrouted_flows(path: str, topology: Topology) -> List[dict]:
    """Return unrouted flow dictionaries (TSpecR fields) from a .ufl or .json file."""
    suffix = Path(path).suffix.lower()
    if suffix == ".ufl":
        return _parse_ufl(path, topology)
    if suffix == ".json":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            raise ValueError(f"Unrouted flow file must contain a JSON list: {path}")
        return data
    raise ValueError(
        f"Unsupported unrouted flow file '{path}'. Use a .ufl file "
        "(or a .json list of the same records)."
    )


def load_routed_flows(path: str, topology: Topology) -> List[dict]:
    """Return routed flow dictionaries from a .rfl or .json file."""
    suffix = Path(path).suffix.lower()
    if suffix == ".rfl":
        return _parse_rfl(path, topology)
    if suffix == ".json":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            raise ValueError(f"Routed flow file must contain a JSON list: {path}")
        return data
    raise ValueError(
        f"Unsupported routed flow file '{path}'. Use a .rfl file "
        "(or a .json list of the same records)."
    )


def _endpoint_kind(topology: Topology, endpoint_id: str) -> Optional[str]:
    node = topology.nodes.get(endpoint_id)
    if node is None:
        return None
    return node.kind


def _warn_endpoint(topology: Topology, endpoint_id: str, role: str, line_number: int) -> None:
    kind = _endpoint_kind(topology, endpoint_id)
    if kind is None:
        logger.warning("Line %s: %s endpoint '%s' is not in the topology", line_number, role, endpoint_id)
    elif kind != "endpoint":
        logger.warning("Line %s: %s node '%s' is not an endpoint (kind: %s)", line_number, role, endpoint_id, kind)


def _parse_ufl(path: str, topology: Topology) -> List[dict]:
    flows: List[dict] = []
    current_source = None
    with open(path, "r", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.lstrip("\ufeff")
            if not line.strip():
                continue
            if line.lstrip().startswith("@"):
                current_source = line.strip()[1:].strip()
                _warn_endpoint(topology, current_source, "source", line_number)
                continue
            if current_source is None:
                logger.warning("Line %s: flow description before a source endpoint", line_number)
                continue
            try:
                flows.extend(_parse_ufl_line(line.strip(), current_source, topology, line_number))
            except ValueError as exc:
                logger.warning("Line %s: %s", line_number, exc)
    logger.info("Parsed %s unrouted flows from %s", len(flows), path)
    return flows


def _parse_ufl_line(line: str, source_id: str, topology: Topology, line_number: int) -> List[dict]:
    """Format: <dst1,dst2,...>: Flows: <count> Class: <c> Type: <t> <rate> <latency_us> <payload>"""
    parts = line.split(":", 1)
    if len(parts) < 2:
        raise ValueError("missing colon after the destination list")

    dest_ids = [dst.strip() for dst in parts[0].split(",") if dst.strip()]
    if not dest_ids:
        raise ValueError("missing destination")
    for dest_id in dest_ids:
        _warn_endpoint(topology, dest_id, "destination", line_number)

    tokens = parts[1].replace("\t", " ").split()
    if "Flows:" not in tokens:
        raise ValueError("missing 'Flows:' field")
    flows_idx = tokens.index("Flows:")
    if flows_idx + 1 >= len(tokens):
        raise ValueError("missing flow count after 'Flows:'")
    flow_count = int(tokens[flows_idx + 1])

    numeric: List[float] = []
    for token in reversed(tokens):
        try:
            numeric.insert(0, float(token))
        except ValueError:
            if numeric:
                break
        if len(numeric) == 3:
            break
    if len(numeric) < 3:
        raise ValueError(f"expected rate, latency_us, and payload at the end of the line, found {numeric}")

    rate_mbps, latency_us, payload_bytes = numeric
    record = {
        "DF_ID": "0",
        "src_id": source_id,
        "dst_id": dest_ids,
        "PCS_R": dict(_PCS_R),
        "D_Max_ns": int(latency_us * 1000),
        "L_Max_bytes": int(payload_bytes) + 70,
        "B_Ref_bytes": 1600,
        "R_Min_Mbps": int(rate_mbps) if float(rate_mbps).is_integer() else rate_mbps,
    }
    return [
        {**record, "dst_id": list(dest_ids), "PCS_R": dict(_PCS_R)}
        for _ in range(flow_count)
    ]


def _endpoint_link_maps(topology: Topology) -> Tuple[Dict[str, str], Dict[str, str]]:
    endpoint_ids = {nid for nid, node in topology.nodes.items() if node.kind == "endpoint"}
    src_to_ingress: Dict[str, str] = {}
    dst_to_egress: Dict[str, str] = {}
    for link_id, link in topology.links.items():
        if link.src in endpoint_ids and link.src not in src_to_ingress:
            src_to_ingress[link.src] = link_id
        if link.dst in endpoint_ids and link.dst not in dst_to_egress:
            dst_to_egress[link.dst] = link_id
    return src_to_ingress, dst_to_egress


def _iie_path(
    src_endpoint: str,
    dst_endpoints: List[str],
    interior_links: str,
    src_to_ingress: Dict[str, str],
    dst_to_egress: Dict[str, str],
) -> str:
    if src_endpoint not in src_to_ingress:
        raise ValueError(f"no ingress link for source endpoint '{src_endpoint}'")
    ingress_link = src_to_ingress[src_endpoint]
    interior_sorted = "-".join(sorted(part for part in interior_links.split("-") if part)) if interior_links else ""
    egress_links = []
    for dst_endpoint in dst_endpoints:
        if dst_endpoint not in dst_to_egress:
            raise ValueError(f"no egress link for destination endpoint '{dst_endpoint}'")
        egress_links.append(dst_to_egress[dst_endpoint])
    egress_sorted = "-".join(sorted(egress_links))
    return f"{ingress_link}^{interior_sorted}^{egress_sorted}"


def _interior_from_path_id(path_id: str) -> str:
    parts = path_id.split("^")
    if len(parts) >= 3:
        return parts[1]
    return ""


def _rfl_number(value) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return str(value)


def render_rfl(flows: List[dict]) -> str:
    """Write admitted flows in the .rfl layout read by load_routed_flows.

    Each line is:
    ``<dst,...>: <interior links>: Flows: <count> Class: 1 Type: 1 <rate> <latency_us> <payload>``
    under an ``@ <source>`` header. Class and Type are accepted by the parser
    and are not stored, so the log writes them as 1. Identical requests are
    collapsed into one line. Latency is microseconds and payload is L_Max
    minus the 70-byte framing the parser adds back.
    """
    counts: Dict[Tuple, int] = {}
    sources: List[str] = []
    lines_by_source: Dict[str, List[Tuple]] = {}
    for flow in flows:
        src = str(flow["src_id"])
        destinations = flow["dst_id"]
        if isinstance(destinations, str):
            destinations = [destinations]
        key = (
            src,
            tuple(destinations),
            _interior_from_path_id(str(flow.get("path_id", ""))),
            flow["SR_Mbps"],
            int(flow["D_Max_ns"]) // 1000,
            int(flow["L_Max_bytes"]) - 70,
        )
        if key not in counts:
            counts[key] = 0
            lines_by_source.setdefault(src, [])
            if src not in sources:
                sources.append(src)
            lines_by_source[src].append(key)
        counts[key] += 1

    lines: List[str] = []
    for src in sources:
        lines.append(f"@ {src}")
        for key in lines_by_source[src]:
            _, destinations, interior, rate, latency_us, payload = key
            lines.append(
                f" {','.join(destinations)}: {interior}: Flows: {counts[key]} "
                f"Class: 1 Type: 1 {_rfl_number(rate)} {latency_us} {payload}"
            )
    if lines:
        lines.append("")
    return "\n".join(lines)


def _parse_rfl_line(line: str) -> Tuple[List[str], str, int, int, int, int]:
    """Format: <dst1,dst2,...>: <interior_links>: Flows: <count> Class: <c> Type: <t> <rate> <latency_us> <payload>"""
    parts = line.strip().replace("\t", " ").split()
    if len(parts) < 8:
        raise ValueError("not enough fields")
    dest_ids = [dst.strip() for dst in parts[0].rstrip(":").split(",") if dst.strip()]
    interior_links = parts[1].rstrip(":")
    if "Flows:" not in parts:
        raise ValueError("missing 'Flows:' field")
    flows_idx = parts.index("Flows:")
    flow_count = int(parts[flows_idx + 1])
    rate_mbps = int(float(parts[-3]))
    latency_us = int(float(parts[-2]))
    payload_size = int(float(parts[-1]))
    return dest_ids, interior_links, flow_count, rate_mbps, latency_us, payload_size


def _parse_rfl(path: str, topology: Topology) -> List[dict]:
    src_to_ingress, dst_to_egress = _endpoint_link_maps(topology)
    flows: List[dict] = []
    flow_id = 1
    current_src = None
    with open(path, "r", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.lstrip("\ufeff")
            if not line.strip():
                continue
            if line.lstrip().startswith("@"):
                header = line.strip().split()
                if len(header) < 2:
                    logger.warning("Line %s: source line has no endpoint id", line_number)
                    current_src = None
                    continue
                current_src = header[1]
                _warn_endpoint(topology, current_src, "source", line_number)
                continue
            if current_src is None:
                logger.warning("Line %s: flow description before a source endpoint", line_number)
                continue
            try:
                dest_ids, interior, count, rate, latency_us, payload = _parse_rfl_line(line)
                path_id = _iie_path(current_src, dest_ids, interior, src_to_ingress, dst_to_egress)
            except ValueError as exc:
                logger.warning("Line %s: %s", line_number, exc)
                continue
            for _ in range(count):
                flows.append({
                    "DF_ID": str(flow_id),
                    "src_id": current_src,
                    "dst_id": list(dest_ids),
                    "path_id": path_id,
                    "PCS_R": dict(_PCS_R),
                    "SR_Mbps": rate,
                    "L_Max_bytes": payload + 70,
                    "B_Ref_bytes": 1600,
                    "D_Max_ns": latency_us * 1000,
                })
                flow_id += 1
    logger.info("Parsed %s routed flows from %s", len(flows), path)
    return flows
