"""Checks for the open-source flow-list and local PSS-file path."""
from __future__ import annotations

import asyncio
from pathlib import Path

from daas_cc.config import ControllerConfig
from daas_cc.domain.models import Topology, Node, Link
from daas_cc.services.controller import DaaSController
from daas_cc.services.flow_lists import load_routed_flows, load_unrouted_flows, render_rfl
from daas_cc.utils.multicast_vlan_mapper import MulticastVLANMapper
from daas_cc.services.pss_config import FlowProfile, IngressProfile, write_endpoint_configs
from daas_cc.services.repository import JsonRepo

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "daas_cc" / "examples"
TOPO = EXAMPLES / "poc-10G-topo.json"


def _topology(tmp_path: Path) -> Topology:
    return JsonRepo(str(tmp_path / "state")).load_topology(str(TOPO))


def test_ufl_matches_the_previous_json_records(tmp_path: Path):
    topo = _topology(tmp_path)
    flows = load_unrouted_flows(str(EXAMPLES / "rdmf-010G-p960a-P1472-mc00.ufl"), topo)
    assert flows
    first = flows[0]
    assert first["src_id"] == "u01l01"
    assert first["dst_id"] == ["u01l02"]
    assert first["R_Min_Mbps"] == 13
    assert first["D_Max_ns"] == 16384000
    assert first["L_Max_bytes"] == 1542
    assert first["B_Ref_bytes"] == 1600
    assert first["PCS_R"]["vlanPrio"] == "7"
    assert len(flows) > 1000


def test_ufl_multicast_destinations_stay_a_list(tmp_path: Path):
    topo = _topology(tmp_path)
    flows = load_unrouted_flows(str(EXAMPLES / "rdmf-010G-p960a-P1472-mc20.ufl"), topo)
    multicast = [flow for flow in flows if len(flow["dst_id"]) > 1]
    assert multicast
    assert multicast[0]["dst_id"][0] != multicast[0]["dst_id"][1]


def test_rfl_builds_an_iie_path(tmp_path: Path):
    topo = Topology()
    topo.add_node(Node(id="u01l01", kind="endpoint", ip_address="10.1.7.2"))
    topo.add_node(Node(id="u01l02", kind="endpoint", ip_address="10.2.8.2"))
    topo.add_node(Node(id="u01l03", kind="endpoint", ip_address="10.3.9.2"))
    topo.add_node(Node(id="lf01", kind="switch"))
    topo.add_link(Link(id="u01l01_l01", src="u01l01", dst="lf01", capacity_Mbps=10000, prop_latency_s=1e-6))
    topo.add_link(Link(id="l02_u01l02", src="lf01", dst="u01l02", capacity_Mbps=10000, prop_latency_s=1e-6))
    topo.add_link(Link(id="l03_u01l03", src="lf01", dst="u01l03", capacity_Mbps=10000, prop_latency_s=1e-6))
    text = "@ u01l01\n u01l02,u01l03: l01_s01: Flows: 2 Class: 1 Type: 1 13 16384 1472\n"
    path = tmp_path / "sample.rfl"
    path.write_text(text, encoding="utf-8")
    flows = load_routed_flows(str(path), topo)
    assert len(flows) == 2
    assert flows[0]["path_id"] == "u01l01_l01^l01_s01^l02_u01l02-l03_u01l03"
    assert flows[0]["SR_Mbps"] == 13
    assert flows[1]["DF_ID"] == "2"


def test_rfl_log_round_trip(tmp_path: Path):
    topo = Topology()
    topo.add_node(Node(id="u01l01", kind="endpoint", ip_address="10.1.7.2"))
    topo.add_node(Node(id="u01l02", kind="endpoint", ip_address="10.2.8.2"))
    topo.add_node(Node(id="u01l03", kind="endpoint", ip_address="10.3.9.2"))
    topo.add_node(Node(id="lf01", kind="switch"))
    topo.add_link(Link(id="u01l01_l01", src="u01l01", dst="lf01", capacity_Mbps=10000, prop_latency_s=1e-6))
    topo.add_link(Link(id="l02_u01l02", src="lf01", dst="u01l02", capacity_Mbps=10000, prop_latency_s=1e-6))
    topo.add_link(Link(id="l03_u01l03", src="lf01", dst="u01l03", capacity_Mbps=10000, prop_latency_s=1e-6))
    original = {
        "src_id": "u01l01",
        "dst_id": ["u01l02", "u01l03"],
        "path_id": "u01l01_l01^l01_s01^l02_u01l02-l03_u01l03",
        "SR_Mbps": 13,
        "L_Max_bytes": 1542,
        "D_Max_ns": 16384000,
    }
    text = render_rfl([original, dict(original)])
    assert text.splitlines()[0] == "@ u01l01"
    assert "u01l02,u01l03: l01_s01: Flows: 2 Class: 1 Type: 1 13 16384 1472" in text
    path = tmp_path / "population.rfl"
    path.write_text(text, encoding="utf-8")
    flows = load_routed_flows(str(path), topo)
    assert len(flows) == 2
    assert flows[0]["path_id"] == original["path_id"]
    assert flows[0]["SR_Mbps"] == 13
    assert flows[0]["L_Max_bytes"] == 1542
    assert flows[0]["D_Max_ns"] == 16384000


def test_multicast_vlan_map_resolves_a_known_tree():
    mapper = MulticastVLANMapper(str(EXAMPLES / "multicast_vlan_mapping.json"))
    vlan = mapper.get_vlan_for_multicast(
        source="u01l01",
        destinations=["u01l02", "u01l03"],
        iie_path="u01l01_l01^l01_s01-s01_l02-s01_l03^l02_u01l02-l03_u01l03",
    )
    assert vlan == "0x118"


def test_cfg_files_use_the_endpoint_name(tmp_path: Path):
    profile = IngressProfile(
        ingress_link_id="u01l01_l01",
        slot_duration_s=1.28e-6,
        period_slots=2,
        bytes_per_slot=1600,
        slots=["b0", "empty"],
        bundle_members={"b0": ["flow-1"]},
        bundle_id_by_flow={"flow-1": "b0"},
        bundle_effective_rates={"b0": 13},
        flows=[
            FlowProfile(
                df_id="flow-1",
                sr_Mbps=13,
                path_link_ids=["u01l01_l01"],
                src_ip="10.1.7.2",
                dst_ip="10.2.8.2",
                latency_ns=16384000,
                l_max_bytes=1542,
                vlan_id="0x1",
                vlan_pri=7,
                path_number=1,
            )
        ],
    )
    written = write_endpoint_configs(tmp_path, profile)
    streams = Path(written["streams_cfg"]).read_text(encoding="utf-8")
    tm = Path(written["tm_cfg"]).read_text(encoding="utf-8")
    assert Path(written["streams_cfg"]).name == "u01l01-STREAMS.cfg"
    assert Path(written["tm_cfg"]).name == "u01l01-TM.cfg"
    assert "10.1.7.2" in streams and "0x1" in streams
    assert "[GBS_TIMESLOT_QUEUE_MAP]" in tm
    assert "0\t1" in tm
    assert "1\t0" in tm


def test_one_unrouted_flow_writes_local_cfg(tmp_path: Path):
    ufl = tmp_path / "one.ufl"
    ufl.write_text("@ u01l01\n u01l02: Flows: 1 Class: 1 Type: 1 13 16384 1472\n", encoding="utf-8")
    output = tmp_path / "pss-output"
    cfg = ControllerConfig(
        topology_file=str(TOPO),
        pre_unrouted_flows_file=str(ufl),
        state_dir=str(tmp_path / "state"),
        pss_output_dir=str(output),
        push_pss_on_startup=False,
        scheduling_policy="flat",
        cost_policy="manual_cost",
        routing_policy="shortest_hops",
        pss_link_rate=10000,
        pss_rate_granularity=1,
        pss_period_slots=10000,
        slot_duration_s=1.28e-6,
    )
    controller = DaaSController(cfg)
    assert len(controller.flows) == 1
    result = asyncio.run(controller.push_all_pss())
    written = [item for item in result.values() if item.get("written")]
    assert written
    streams = Path(written[0]["files"]["streams_cfg"]).read_text(encoding="utf-8")
    assert "10.1.7.2" in streams
    assert output.exists()
    saved = controller.save_flow_table_log(str(tmp_path / "population.json"))
    log_path = Path(saved["file_path"])
    assert log_path.suffix == ".rfl"
    assert saved["flow_count"] == 1
    logged = load_routed_flows(str(log_path), controller.topo)
    assert len(logged) == 1
    admitted = controller.flows[0]
    assert logged[0]["src_id"] == admitted.src_id
    assert logged[0]["dst_id"] == admitted.dst_id
    assert logged[0]["path_id"] == admitted.path_id
    assert logged[0]["SR_Mbps"] == admitted.sr_Mbps


def test_flow_table_load_parses_an_rfl_file(tmp_path: Path):
    rfl = tmp_path / "table.rfl"
    rfl.write_text(
        "@ u01l01\n u01l02: l01_s01-s01_l02: Flows: 2 Class: 1 Type: 1 13 16384 1472\n",
        encoding="utf-8",
    )
    cfg = ControllerConfig(
        topology_file=str(TOPO),
        state_dir=str(tmp_path / "state"),
        push_pss_on_startup=False,
        scheduling_policy="flat",
        cost_policy="manual_cost",
        routing_policy="shortest_hops",
        pss_link_rate=10000,
        pss_rate_granularity=1,
        pss_period_slots=10000,
        slot_duration_s=1.28e-6,
    )
    controller = DaaSController(cfg)
    rejected = controller.load_flow_table_log(str(tmp_path / "table.json"))
    assert rejected["accepted"] is False
    loaded = controller.load_flow_table_log(str(rfl))
    assert loaded["accepted"] is True
    assert loaded["flows_in_file"] == 2
    assert loaded["flows_loaded"] == 2
    assert len(controller.flows) == 2
    assert controller.flows[0].path_id == "u01l01_l01^l01_s01-s01_l02^l02_u01l02"
    assert controller.flows[0].src_id == "u01l01"
    assert controller.flows[0].dst_id == ["u01l02"]
