from __future__ import annotations
from fastapi import FastAPI, HTTPException, Query, Request
from contextlib import asynccontextmanager
from typing import Literal, Optional
from ..config import ControllerConfig
from .controller import DaaSController
from ..domain.models import TSpecR
import logging


def make_app(cfg: ControllerConfig) -> FastAPI:
    ctrl = DaaSController(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Lifespan context manager for startup and shutdown tasks."""
        # Startup
        if cfg.push_pss_on_startup:
            logging.info("Startup: push_pss_on_startup is enabled")
            logging.info("Startup: Computing PSS and writing *-TM.cfg and *-STREAMS.cfg ...")
            try:
                result = await ctrl.push_all_pss()
                successful = sum(1 for r in result.values() if isinstance(r, dict) and r.get("written", False))
                total = sum(1 for r in result.values() if isinstance(r, dict) and "written" in r)
                logging.info("Startup: PSS files written for %s/%s ingresses", successful, total)
            except Exception as e:
                import traceback
                logging.error("Startup: Error writing PSS files: %s", e)
                logging.error(traceback.format_exc())
        else:
            logging.info("Startup: push_pss_on_startup is disabled - skipping PSS file write")

        yield

        # Shutdown (if needed in future)
        logging.info("Shutdown: DaaS-CC API shutting down")

    app = FastAPI(title="DaaS-CC NBI/WSI (REST)", lifespan=lifespan)

    @app.get("/state")
    def state():
        """Get network state summary (optimized for large flow populations)"""
        return {
            "links_residual_Mbps": {k: v for k, v in ctrl.residual_Mbps.items()},
            "total_flows": len(ctrl.flows),
            "flows_summary": {
                "note": "Use GET /flows/all for complete flow list (may be slow for large populations)",
                "use_pagination": "GET /state/flows?limit=100&offset=0 for paginated access"
            }
        }

    @app.get("/state/summary")
    def state_summary():
        """Get statistical summary of network state (fast, no flow details)"""
        # Calculate statistics
        total_flows = len(ctrl.flows)
        flows_by_src = {}
        flows_by_dst = {}
        total_rate_mbps = 0

        for flow in ctrl.flows:
            flows_by_src[flow.src_id] = flows_by_src.get(flow.src_id, 0) + 1
            # dst_id is now a list - count flows for each destination
            dst_ids = flow.dst_id if isinstance(flow.dst_id, list) else [flow.dst_id]
            for dst in dst_ids:
                flows_by_dst[dst] = flows_by_dst.get(dst, 0) + 1
            total_rate_mbps += flow.sr_Mbps

        # Calculate link utilization
        link_utilization = {}
        for link_id, link in ctrl.topo.links.items():
            capacity = link.capacity_Mbps
            deterministic_capacity = capacity * cfg.deterministic_capacity_fraction
            residual = ctrl.residual_Mbps.get(link_id, 0)
            used = deterministic_capacity - residual
            utilization_pct = (used / capacity * 100) if capacity > 0 else 0
            link_utilization[link_id] = {
                "capacity_Mbps": capacity,
                "deterministic_capacity_Mbps": deterministic_capacity,
                "used_Mbps": used,
                "residual_Mbps": residual,
                "utilization_percent": round(utilization_pct, 2)
            }

        return {
            "total_flows": total_flows,
            "total_allocated_rate_Mbps": total_rate_mbps,
            "flows_by_source": flows_by_src,
            "flows_by_destination": flows_by_dst,
            "total_links": len(ctrl.topo.links),
            "link_utilization": link_utilization,
            "endpoints": {
                "total": len([n for n in ctrl.topo.nodes.values() if n.kind == 'endpoint']),
                "sources": list(flows_by_src.keys()),
                "destinations": list(flows_by_dst.keys())
            }
        }

    @app.get("/state/full")
    def state_full():
        """Get complete network state including all flows (WARNING: may be slow for large flow populations)"""
        return {
            "links_residual_Mbps": {k: v for k, v in ctrl.residual_Mbps.items()},
            "total_flows": len(ctrl.flows),
            "flows": [f.model_dump() for f in ctrl.flows],
        }

    @app.get("/state/flows")
    def state_flows_paginated(limit: int = 100, offset: int = 0):
        """Get paginated list of flows from network state"""
        total = len(ctrl.flows)
        flows_page = ctrl.flows[offset:offset + limit]
        return {
            "total_flows": total,
            "limit": limit,
            "offset": offset,
            "returned": len(flows_page),
            "has_more": offset + limit < total,
            "next_offset": offset + limit if offset + limit < total else None,
            "flows": [f.model_dump() for f in flows_page]
        }

    @app.get("/debug/version")
    def debug_version():
        """Check if cost management code is loaded"""
        from daas_cc.domain.models import Flow, TSpecR
        flow_fields = list(Flow.model_fields.keys())
        tspecr_fields = list(TSpecR.model_fields.keys())

        # Also check what the flows_add endpoint sees
        import inspect
        flows_add_func = app.routes[-1].endpoint  # Get the flows_add function
        sig = inspect.signature(flows_add_func)
        spec_annotation = sig.parameters.get('spec')

        return {
            "cost_management_loaded": True,
            "flow_fields": flow_fields,
            "tspecr_fields": tspecr_fields,
            "has_unit_cost_in_flow": "unit_cost" in flow_fields,
            "has_unit_cost_in_tspecr": "unit_cost" in tspecr_fields,
            "has_running_cost": "running_cost" in flow_fields,
            "has_time_last_update": "time_last_update" in flow_fields,
            "tspecr_class_id": id(TSpecR),
            "tspecr_module": TSpecR.__module__
        }

    @app.post("/flows/add")
    async def flows_add(spec: TSpecR, count: int = 1, request: Request = None):
        """
        Add flow(s) to the network.

        Args:
            spec: Flow specification (TSpecR)
            count: Number of identical flows to add (default: 1)
        """
        # DEBUG: See what's in the raw request
        if request:
            body = await request.body()
            import json
            try:
                body_json = json.loads(body)
                print(f"[DEBUG] Raw request body: {body_json}")
                print(f"[DEBUG] 'unit_cost' in request: {'unit_cost' in body_json}")
                if 'unit_cost' in body_json:
                    print(f"[DEBUG] Request unit_cost value: {body_json['unit_cost']}")
            except:
                pass

        print(f"[DEBUG] Parsed spec model_dump(): {spec.model_dump()}")
        print(f"[DEBUG] TSpecR.model_fields.keys(): {list(TSpecR.model_fields.keys())}")

        try:
            if count < 1:
                raise HTTPException(status_code=400, detail="count must be >= 1")

            if count == 1:
                # Single flow addition (original behavior)
                res = ctrl.add_flow(spec)
                if not res.get("accepted", False):
                    logging.warning("Flow admission rejected: %s", res.get("reason"))
                    raise HTTPException(status_code=409, detail=res["reason"])
                return res
            else:
                # Batch flow addition
                res = ctrl.add_flows_batch(spec, count)

                if res["accepted_count"] == 0:
                    # No flows accepted
                    logging.warning(f"Batch flow admission rejected: {res.get('reason')}")
                    raise HTTPException(
                        status_code=409,
                        detail=f"No flows could be admitted. Reason: {res.get('reason')}"
                    )
                elif res["rejected_count"] > 0:
                    # Some flows accepted, some rejected
                    logging.info(
                        f"Batch flow admission partial success: {res['accepted_count']}/{res['total_requested']} accepted, "
                        f"{res['rejected_count']} rejected. Reason: {res.get('reason')}"
                    )

                return res
        except HTTPException:
            raise
        except Exception as e:
            logging.error(f"Error in flows_add: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")

    @app.get("/flows/all")
    def get_all_flows(limit: int = None, offset: int = 0):
        """Get TSpecs of all flows currently in the network (supports optional pagination)"""
        total = len(ctrl.flows)

        # If no limit specified and there are many flows, suggest pagination
        if limit is None:
            if total > 1000:
                return {
                    "total_flows": total,
                    "warning": f"Large flow population ({total} flows). Consider using pagination.",
                    "suggestion": "Add ?limit=100&offset=0 to URL for paginated results",
                    "flows": []
                }
            else:
                # Return all flows if population is small
                limit = total

        flows_page = ctrl.flows[offset:offset + limit] if limit else ctrl.flows
        flows_tspecs = []
        for flow in flows_page:
            # Update cost before displaying
            ctrl.update_flow_cost(flow)
            flow_data = {
                "DF_ID": flow.df_id,
                "src_id": flow.src_id,
                "dst_id": flow.dst_id,
                "PCS_R": flow.pcs,
                "D_Max_ns": flow.d_max_ns,
                "L_Max_bytes": flow.l_max_bytes,
                "B_Ref_bytes": flow.b_ref_bytes,
                "R_Min_Mbps": flow.sr_Mbps,
                "path_id": flow.path_id,
                "unit_cost": flow.unit_cost,
                "running_cost": flow.running_cost,
                "cost_tracking_initialized": flow.time_last_update is not None
            }
            # Include VLAN ID for multicast flows
            if flow.vlan_id:
                flow_data["vlan_id"] = flow.vlan_id
            flows_tspecs.append(flow_data)

        result = {
            "total_flows": total,
            "returned": len(flows_tspecs),
            "flows": flows_tspecs
        }

        # Add pagination info if limit was specified
        if limit is not None:
            result["limit"] = limit
            result["offset"] = offset
            result["has_more"] = offset + limit < total
            result["next_offset"] = offset + limit if offset + limit < total else None

        return result

    @app.get("/flows/multicast-only")
    def get_multicast_flows_fast():
        """Return multicast flows without the cost calculation."""
        mc_flows = []
        for flow in ctrl.flows:
            # Check if multicast (dst_id is list with >1 element)
            if isinstance(flow.dst_id, list) and len(flow.dst_id) > 1:
                mc_flows.append({
                    "DF_ID": flow.df_id,
                    "src_id": flow.src_id,
                    "dst_id": flow.dst_id,
                    "PCS_R": flow.pcs,
                    "D_Max_ns": flow.d_max_ns,
                    "L_Max_bytes": flow.l_max_bytes,
                    "B_Ref_bytes": flow.b_ref_bytes,
                    "R_Min_Mbps": flow.sr_Mbps,
                    "path_id": flow.path_id,
                    "unit_cost": flow.unit_cost,
                })
        return {
            "total_flows": len(ctrl.flows),
            "total_multicast": len(mc_flows),
            "flows": mc_flows
        }

    @app.get("/ref_unit_cost/config")
    def get_reference_unit_cost_config():
        """
        Get the current reference unit cost configuration.

        The reference unit cost is the starting value from which the unit cost is derived
        for a flow at the time when the flow is provisioned.

        Returns:
            Current reference_unit_cost value and cost_policy configuration
        """
        return {
            "reference_unit_cost": ctrl.runtime_reference_unit_cost,
            "cost_policy": ctrl.cfg.cost_policy,
            "config_reference_unit_cost": ctrl.cfg.reference_unit_cost,
            "description": "Reference unit cost in USD per second"
        }

    @app.post("/ref_unit_cost/modify")
    def modify_reference_unit_cost(
        reference_unit_cost: float = Query(
            ...,
            ge=0.0,
            description="New reference unit cost value (USD/s, must be non-negative)"
        )
    ):
        """
        Modify the reference unit cost at runtime.

        This changes the reference unit cost used for all flows provisioned AFTER this change.
        Pre-existing flows keep their current unit_cost values.

        Args:
            reference_unit_cost: New reference unit cost value (must be >= 0.0)

        Returns:
            Confirmation of the new reference_unit_cost value
        """
        old_value = ctrl.runtime_reference_unit_cost
        ctrl.runtime_reference_unit_cost = reference_unit_cost

        return {
            "status": "success",
            "old_reference_unit_cost": old_value,
            "new_reference_unit_cost": reference_unit_cost,
            "cost_policy": ctrl.cfg.cost_policy,
            "note": "Only flows provisioned after this change will use the new reference unit cost"
        }

    @app.get("/flows/{df_id}")
    def get_flow(df_id: str):
        """Get full TSpec of a specific flow by DF_ID"""
        flow = ctrl.get_flow(df_id)
        if not flow:
            raise HTTPException(status_code=404, detail="Flow not found")

        # Update cost before returning
        ctrl.update_flow_cost(flow)

        # Return flow as TSpecR format with current values
        return {
            "DF_ID": flow.df_id,
            "src_id": flow.src_id,
            "dst_id": flow.dst_id,
            "PCS_R": flow.pcs,
            "D_Max_ns": flow.d_max_ns,
            "L_Max_bytes": flow.l_max_bytes,
            "B_Ref_bytes": flow.b_ref_bytes,
            "R_Min_Mbps": flow.sr_Mbps,
            "path_id": flow.path_id,
            "unit_cost": flow.unit_cost,
            "running_cost": flow.running_cost,
            "cost_tracking_initialized": flow.time_last_update is not None
        }

    @app.get("/flows/src_endpoint/{src_id}")
    def get_flows_by_source(src_id: str):
        """Get TSpecs of all flows originating from a specific source endpoint"""
        flows_tspecs = []
        for flow in ctrl.flows:
            if flow.src_id == src_id:
                # Update cost before displaying
                ctrl.update_flow_cost(flow)
                flows_tspecs.append({
                    "DF_ID": flow.df_id,
                    "src_id": flow.src_id,
                    "dst_id": flow.dst_id,
                    "PCS_R": flow.pcs,
                    "D_Max_ns": flow.d_max_ns,
                    "L_Max_bytes": flow.l_max_bytes,
                    "B_Ref_bytes": flow.b_ref_bytes,
                    "R_Min_Mbps": flow.sr_Mbps,
                    "path_id": flow.path_id,
                    "unit_cost": flow.unit_cost,
                    "running_cost": flow.running_cost,
                    "cost_tracking_initialized": flow.time_last_update is not None
                })
        return {
            "src_id": src_id,
            "total_flows": len(flows_tspecs),
            "flows": flows_tspecs
        }

    @app.get("/flows/dst_endpoint/{dst_id}")
    def get_flows_by_destination(dst_id: str):
        """Get TSpecs of all flows terminating at a specific destination endpoint"""
        flows_tspecs = []
        for flow in ctrl.flows:
            # dst_id is now a list - check if dst_id is in the list
            dst_ids = flow.dst_id if isinstance(flow.dst_id, list) else [flow.dst_id]
            if dst_id in dst_ids:
                # Update cost before displaying
                ctrl.update_flow_cost(flow)
                flows_tspecs.append({
                    "DF_ID": flow.df_id,
                    "src_id": flow.src_id,
                    "dst_id": flow.dst_id,
                    "PCS_R": flow.pcs,
                    "D_Max_ns": flow.d_max_ns,
                    "L_Max_bytes": flow.l_max_bytes,
                    "B_Ref_bytes": flow.b_ref_bytes,
                    "R_Min_Mbps": flow.sr_Mbps,
                    "path_id": flow.path_id,
                    "unit_cost": flow.unit_cost,
                    "running_cost": flow.running_cost,
                    "cost_tracking_initialized": flow.time_last_update is not None
                })
        return {
            "dst_id": dst_id,
            "total_flows": len(flows_tspecs),
            "flows": flows_tspecs
        }

    @app.get("/links/all")
    def get_all_links():
        """Get information about all network links"""
        links_info = {}
        for link_id, link in ctrl.topo.links.items():
            capacity = link.capacity_Mbps
            deterministic_capacity = capacity * cfg.deterministic_capacity_fraction
            residual = ctrl.residual_Mbps.get(link_id, 0)
            used = deterministic_capacity - residual
            utilization_pct = (used / capacity * 100) if capacity > 0 else 0

            links_info[link_id] = {
                "capacity_Mbps": capacity,
                "deterministic_capacity_Mbps": deterministic_capacity,
                "used_Mbps": used,
                "residual_Mbps": residual,
                "utilization_percent": round(utilization_pct, 2)
            }
        return {
            "total_links": len(links_info),
            "links": links_info
        }

    @app.get("/links/{link_id}")
    def get_link(link_id: str):
        """Get information about a specific network link"""
        link = ctrl.topo.links.get(link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Link not found")

        capacity = link.capacity_Mbps
        deterministic_capacity = capacity * cfg.deterministic_capacity_fraction
        residual = ctrl.residual_Mbps.get(link_id, 0)
        used = deterministic_capacity - residual
        utilization_pct = (used / capacity * 100) if capacity > 0 else 0

        return {
            link_id: {
                "capacity_Mbps": capacity,
                "deterministic_capacity_Mbps": deterministic_capacity,
                "used_Mbps": used,
                "residual_Mbps": residual,
                "utilization_percent": round(utilization_pct, 2)
            }
        }

    @app.post("/flows/modify")
    def flows_modify(df_id: str, spec: TSpecR):
        res = ctrl.modify_flow(df_id, spec)
        if not res.get("accepted", False):
            logging.warning("Flow modification failed: %s", res.get("reason", "not found"))
            raise HTTPException(status_code=409, detail=res["reason"])
        return res

    @app.delete("/flows/{df_id}")
    def flows_delete(df_id: str):
        res = ctrl.remove_flow(df_id)
        if not res.get("removed", False):
            logging.warning("Flow removal failed: %s", res.get("reason", "not found"))
            raise HTTPException(status_code=404, detail=res.get("reason", "not found"))
        return res

    @app.post("/pss/push")
    async def push_all_pss_configs():
        try:
            res = await ctrl.push_all_pss()
            return res
        except Exception as e:
            logging.error("Failed to write PSS configurations: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to write PSS configurations")

    @app.post("/pss/push-new")
    async def push_changed_pss_configs():
        """Write cfg files only for ingresses whose profile changed since the last write."""
        try:
            res = await ctrl.push_changed_pss()
            return res
        except Exception as e:
            logging.error("Failed to write changed PSS configurations: %s", str(e))
            raise HTTPException(status_code=500, detail="Failed to write changed PSS configurations")

    @app.get("/scheduler/config")
    def get_scheduler_config():
        """
        Get the current scheduler configuration.

        Returns:
            Current scheduling policy and other scheduler-related settings
        """
        return {
            "scheduling_policy": ctrl.cfg.scheduling_policy,
            "slot_duration_s": ctrl.cfg.slot_duration_s,
            "pss_period_slots": ctrl.cfg.pss_period_slots,
            "pss_link_rate": ctrl.cfg.pss_link_rate,
            "pss_rate_granularity": ctrl.cfg.pss_rate_granularity,
            "available_policies": ["ras", "flat", "plain_hierarchical"]
        }

    @app.post("/scheduler/modify")
    async def modify_scheduler(
        scheduling_policy: Literal["ras", "flat", "plain_hierarchical"] = Query(
            ...,
            description="The scheduling policy to use for PSS computation"
        )
    ):
        """
        Modify the scheduling policy at runtime.

        If the policy changes, PSS is recomputed for all ingress links and written to local cfg files.
        If the policy is the same as the current one, no action is taken.

        Args:
            scheduling_policy: The new scheduling policy ("ras", "flat", or "plain_hierarchical")

        Returns:
            Status of the policy change and PSS file write
        """

        try:
            res = await ctrl.modify_scheduling_policy(scheduling_policy)
            return res
        except Exception as e:
            logging.error("Failed to modify scheduling policy: %s", str(e))
            raise HTTPException(status_code=500, detail=f"Failed to modify scheduling policy: {str(e)}")

    from pathlib import Path as _Path
    _default_log_name = _Path(ctrl._derive_log_file_path()).name

    @app.post("/flow_table/log")
    def save_flow_table_log(
        file_name: str = Query(
            default=_default_log_name,
            description=(
                "Name of the log file to write. Relative names are resolved relative to "
                "the topology file directory (examples/). "
                "Default: routed-<topology-stem>-log.rfl."
            )
        )
    ):
        """Save the current in-memory flow table as a routed flow list (.rfl).

        The default file name is derived from the topology file name:
        routed-<topology-stem>-log.rfl. The file_name parameter can be edited
        to save under a different name. A name that does not end in .rfl is
        given that suffix.
        """
        try:
            result = ctrl.save_flow_table_log(file_name=file_name)
            return result
        except Exception as e:
            logging.error("Failed to save flow table log: %s", str(e), exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to save flow table log: {str(e)}")

    @app.post("/flow_table/load")
    def load_flow_table_log(
        file_name: str = Query(
            ...,
            description=(
                "Name of a .rfl routed flow list. Relative names are resolved "
                "next to the topology file (examples/)."
            )
        )
    ):
        """Load a .rfl routed flow list into the current flow table.

        The file uses the same layout written by POST /flow_table/log. By default,
        loading is only allowed when no flows are currently allocated. Set
        'allow_load_log_with_existing_flows': true in the config to append.
        """
        try:
            result = ctrl.load_flow_table_log(file_name)
            if not result.get("accepted", False):
                raise HTTPException(status_code=409, detail=result["reason"])
            return result
        except HTTPException:
            raise
        except Exception as e:
            logging.error("Failed to load flow table log: %s", str(e), exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to load flow table log: {str(e)}")

    return app
