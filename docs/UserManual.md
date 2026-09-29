# DaaS-CC user manual

This manual explains how to install, configure, and operate the Determinism-as-a-Service centralized controller. It is not the repository README.

The controller reads a topology, admits routed and unrouted flows, and writes local shaper files. Its operator interface is the HTTP API. Open `http://localhost:8000/docs` after startup for the interactive page.

Example inputs live in `daas_cc/examples/`. A file name in the config that is not an absolute path is resolved in that directory. `pss_output_dir` is the exception: a relative value is resolved from the working directory.

## 1. Installation prerequisites

- A bash shell. On Windows, use WSL.
- Python 3.9 or newer, available as `python3`.
- The standard-library `venv` module. On Debian and Ubuntu, install it with:

```bash
sudo apt install python3-venv python3-pip
```

- Network access the first time packages are downloaded.

Do not install the controller into the system Python. Debian and Ubuntu refuse that install. `check_dependencies.sh` creates a virtual environment in `.venv` at the repository root and installs there.

The Python packages are declared in `pyproject.toml`: Pydantic, NetworkX, python-dotenv, FastAPI, and Uvicorn. The `test` extra also installs pytest. You do not install these by hand.

## 2. Installation procedure

From the repository root:

```bash
./check_dependencies.sh
```

Run that script in these cases:

- Once, before the first start.
- Again after `pyproject.toml` changes.
- Again if `.venv` is missing or its `pip` does not run. The script recreates the environment in that case.

Do not run it before every start. An editable install already sees source changes, and `start_daas_cc.sh` does not install packages.

The script creates `.venv` when needed, upgrades `pip` inside it, and installs the package with the `api` and `test` extras. `.venv` stays on the machine and is not part of the source tree.

## 3. Traffic engineering and management functions

Three functions turn a flow request into a schedule. Routing chooses the path. Admission control decides whether that path can carry the flow inside its latency and rate bounds, and which shaping rate to reserve. Scheduling turns the admitted flows at one ingress into the periodic timeslot schedule written for the shaper.

The names below are the values of `routing_policy`, `admission_mode`, and `scheduling_policy` in the config file. Routing and admission apply when a flow is admitted. The scheduling policy can also be changed on a running controller with `POST /scheduler/modify`, which rebuilds the schedules.

All three routing policies keep only paths within `routing_max_hop_num` hops and `routing_max_latency_num_us` microseconds of propagation delay, and they keep at most `routing_max_path_num` candidates per source-destination pair.

### 3.1 Routing

In the data-center example included with this repository, the code base assumes that network routes are identified by VLAN IDs. Applying the solution to a network where the routes of deterministic flows are identified by different means, for example MPLS labels, requires expanding the code base to support that method.

Routing applies to unrouted requests (a `.ufl` file or `POST /flows/add`). A `.rfl` file already names the path, so these policies are not used for those flows.

| Policy | What it does |
| --- | --- |
| `shortest_hops` | Prefers the path with the fewest hops. Equal hop counts are broken by lower propagation delay. |
| `shortest_latency` | Prefers the path with the lowest propagation delay. Equal delays are broken by fewer hops. |
| `largest_residual_bandwidth` | Prefers the feasible path whose tightest interior link still has the most spare capacity. Links that appear in every remaining candidate are ignored, so the comparison is on the links that actually differ. The order is recomputed as flows are admitted and spare capacity changes. |
| `largest_residual_bandwidth_minimum_tree` | Uses the same spare-capacity order for each destination. A multicast flow is built as one tree: each new destination reuses the longest prefix already in the tree and adds only the extra links needed to reach it. |

`shortest_hops` and `shortest_latency` do not look at how loaded the links are. The two residual-bandwidth policies do, so a later flow can take a different path from an earlier one with the same endpoints.

### 3.2 Admission control

Admission walks the candidate paths in the order routing produced and accepts the first path that can meet the flow's minimum rate and deadline. The reserved shaping rate is at least `R_Min`. It is raised when a higher rate is required so that the deadline still holds after the network traversal delay. `bandwidth_overhead` adds a percentage on top of that rate. Every link on the path must have that much residual capacity left. If no candidate passes, the flow is rejected.

| Mode | What it does |
| --- | --- |
| `exact` | Estimates network delay from the path itself: propagation delay plus a queuing term that depends on how many ingresses can feed each link on that path. |
| `conservative` | Ignores the particular path and uses a domain-wide upper bound: `conservative_cac_max_path_hops`, every ingress in the topology, and `conservative_cac_max_link_latency_s`. That delay bound is pessimistic, so the flow is reserved a higher rate or rejected sooner. |

A multicast flow checks the new links of the tree. Links already in the tree are reused and are not reserved a second time.

### 3.3 Scheduling

Scheduling runs after admission. Flows that share an ingress, a path, a shaping rate, and a maximum packet size are packed into bundles that fit in one timeslot. The policy decides which bundle, or the empty queue, owns each timeslot of the period. The result is the `*-TM.cfg` file for that ingress.

| Policy | What it does |
| --- | --- |
| `ras` | Two levels. The top level chooses among paths and prefers a path whose links are all allowed to send in that timeslot. The bottom level then chooses a bundle on the chosen path. If no path is fully eligible, the path with the earliest finish time is used. This is the routing-aware schedule the shaper is built for. |
| `plain_hierarchical` | The same two levels, paths and then bundles, but the path is chosen only by finish time. Link eligibility is not used to prefer one path over another. |
| `flat` | No path level. Each bundle is scheduled on its own. Link eligibility is not consulted. |

## 4. Configuration

The default controller config is `daas_cc/examples/poc-10G-config.json`. Select another file with `--config` / `-c` or with the environment variable `DAAS_CONFIG_FILE`.

Two labels in the example file, `use_case` and `description`, are notes for the reader. The controller does not read them.

### 4.1 `*-config.json`


| Field                                 | Required | Meaning                                                                                                                                                                                      |
| ------------------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `topology_file`                       | yes      | Topology file. A relative name is resolved under `daas_cc/examples/`.                                                                                                                        |
| `pre_routed_flows_file`               | no       | `.rfl` file of flows whose paths are already chosen. Loaded at startup. `null` skips it.                                                                                                     |
| `pre_unrouted_flows_file`             | no       | `.ufl` file of flow requests. The controller chooses each path. Loaded at startup. `null` skips it.                                                                                          |
| `flow_table_log_file`                 | no       | Extra `.rfl` file loaded at startup after the two files above, using the same routed-flow rules.                                                                                             |
| `multicast_vlan_mapping_file`         | no       | Multicast path-to-VLAN map. A relative name is resolved under `daas_cc/examples/`.                                                                                                           |
| `state_dir`                           | no       | Directory for controller state files. Default `state`.                                                                                                                                       |
| `pss_output_dir`                      | no       | Directory for `*-TM.cfg` and `*-STREAMS.cfg`. This is the only place that directory is set. Default `pss-output`. A relative path is resolved from the working directory.                    |
| `allow_load_log_with_existing_flows`  | no       | When `false` (the default), `POST /flow_table/load` is refused if any flow is already admitted. Set `true` to append.                                                                        |
| `deterministic_capacity_fraction`     | no       | Fraction of each link capacity available to admitted flows. Default `1.0`.                                                                                                                   |
| `slot_duration_s`                     | no       | Scheduler timeslot length in seconds. Default `0.000000512` (for 25 Gbps). The 10 Gbps example uses `1.28e-6`.                                                                               |
| `pss_link_rate`                       | no       | Reference link rate in Mb/s. Default `25000`. The 10 Gbps example uses `10000`.                                                                                                              |
| `pss_rate_granularity`                | no       | Rate granularity in Mb/s. Default `1`.                                                                                                                                                       |
| `pss_period_slots`                    | computed | When both `pss_link_rate` and `pss_rate_granularity` are present, the controller sets this to their integer ratio and ignores a value written in the file. Otherwise the default is `25000`. |
| `push_pss_on_startup`                 | no       | When `true` (the default), startup computes the schedule and writes the PSS files.                                                                                                           |
| `flow_routing_progress_interval_s`    | no       | Seconds between progress lines while unrouted flows are admitted. `0` disables the lines. Default `5.0`.                                                                                     |
| `scheduling_policy`                   | no       | `ras`, `flat`, or `plain_hierarchical`. Default `ras`. The 10 Gbps example uses `flat`.                                                                                                      |
| `cost_policy`                         | no       | `manual_cost` or `congestion_cost`. Default `manual_cost`.                                                                                                                                   |
| `reference_unit_cost`                 | no       | Starting unit cost in USD/s. Default `0.0`.                                                                                                                                                  |
| `routing_policy`                      | no       | `shortest_hops`, `shortest_latency`, `largest_residual_bandwidth`, or `largest_residual_bandwidth_minimum_tree`. Default `largest_residual_bandwidth`.                                       |
| `routing_max_path_num`                | no       | Maximum candidate paths kept per source-destination pair. Default `8`.                                                                                                                       |
| `routing_max_hop_num`                 | no       | Maximum hops considered by the routing policies. Default `4`.                                                                                                                                |
| `routing_max_latency_num_us`          | no       | Maximum path latency in microseconds considered by the routing policies. Default `1000000.0`.                                                                                                |
| `admission_mode`                      | no       | `exact` or `conservative`. Default `exact`.                                                                                                                                                  |
| `bandwidth_overhead`                  | no       | Extra reservation, as a percentage. `5` means 5 percent. Default `0`.                                                                                                                        |
| `conservative_cac_max_path_hops`      | no       | Hop bound used when `admission_mode` is `conservative`. Default `4`.                                                                                                                         |
| `conservative_cac_max_link_latency_s` | no       | Per-link latency bound in seconds used when `admission_mode` is `conservative`. Default `0.000010`.                                                                                          |


Only one of `pre_routed_flows_file` and `pre_unrouted_flows_file` needs to be set. The 10 Gbps examples switch traffic mix by pointing one of those fields at `rdmf-010G-p960a-P1472-mc00` through `mc80`, using `.ufl` when the controller must route and `.rfl` when the paths are already written down.

### 4.2 `*-topo.json`

The shipped topology is `daas_cc/examples/poc-10G-topo.json`.


| Field           | Meaning                                                                                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `nodes`         | Each node has `id`, `kind` (`endpoint` or `switch`), optional `ip_address`, and `attrs`. Endpoint addresses are data-plane addresses written into `*-STREAMS.cfg`. |
| `links`         | Each link has `id`, `src`, `dst`, `capacity_Mbps`, and `prop_latency_s`. `src` and `dst` are node ids.                                                             |
| `ingress_links` | Link ids that enter the network from an endpoint. The controller builds one PSS instance per ingress link.                                                         |
| `paths`         | Unicast path id to VLAN id. Each entry is `{"id": "<path>", "vlan_id": "0x1"}`.                                                                                    |


A path id has the form `ingress^interior^egress`. Several links inside one segment (interior or egress, since ingress always has one link only) are separated by `-`. Within each segment, the links must be listed in alphabetical order. The shipped table covers unicast paths. Multicast VLAN ids come from `multicast_vlan_mapping.json`. If that map has no entry for a multicast path, a matching `paths` entry is used. If neither has an entry, the flow is still admitted on the path the router already selected, and the controller logs one warning for that path. The warning does not change the route. In the stream file the flow is written with VLAN `0x0` and destination address `224.0.0.1`.

### 4.3 `multicast_vlan_mapping.json`

`daas_cc/examples/multicast_vlan_mapping.json` maps a multicast tree to a VLAN id. The object keys are VLAN ids, from `0x100` upward. Each value can contain:


| Field           | Role                                                                                                  |
| --------------- | ----------------------------------------------------------------------------------------------------- |
| `network_paths` | Path strings the controller matches against the admitted tree. This is the field used for the lookup. |
| `mgid`          | Multicast group id stored with the entry.                                                             |
| `ports`         | Switch ports stored with the entry.                                                                   |
| `source`        | Source label stored with the entry.                                                                   |
| `destinations`  | Destination endpoint ids stored with the entry.                                                       |
| `lsl_path`      | Alternate path notation stored with the entry.                                                        |


After routing, the controller looks up the flow's `ingress^interior^egress` path in `network_paths`. The same VLAN can list more than one path. A tree that is not an exact listed path does not receive a VLAN from this file.

### 4.4 Unrouted flow table (`.ufl`)

A `.ufl` file asks the controller to choose the path. The shipped files are `daas_cc/examples/rdmf-010G-p960a-P1472-mc00.ufl` through `mc80.ufl`. Set `pre_unrouted_flows_file` to the one you want.

```text
@ u01l01
 u01l02: Flows: 80 Class: 1 Type: 1 13 16384 1472
 u01l02,u01l03: Flows: 1 Class: 1 Type: 1 13 16384 1472
```

- A line `@ <endpoint>` sets the source for the following requests.
- A request line is `<destinations>: Flows: <count> Class: <class> Type: <type> <rate> <latency_us> <payload>`.
- Destinations are endpoint ids separated by commas. One destination is unicast. More than one is multicast.
- `Flows` is how many identical requests to admit.
- `Class` and `Type` are accepted and not stored.
- `<rate>` is `R_Min` in Mb/s. An integral value is stored as an integer.
- `<latency_us>` is the deadline in microseconds. The controller stores nanoseconds (`latency_us` × 1000).
- `<payload>` is the payload size in bytes. The controller stores `L_Max` as payload + 70 bytes of framing.
- `B_Ref` is 1600 bytes for every request.
- Packet classification is fixed: VLAN priority 7, DSCP 46, source port 0, destination port 0.

Admitting a large `.ufl` file recomputes routes as bandwidth is consumed. The first startup of `mc00` can take several minutes. Progress lines use `flow_routing_progress_interval_s`.

### 4.5 Routed flow table (`.rfl`)

A `.rfl` file names the interior of each path. The controller looks up the ingress link from the source endpoint and the egress links from the destinations. Set `pre_routed_flows_file` to load one at startup, or pass the file to `POST /flow_table/load`.

```text
@ u01l01
 u01l02: l01_s01-s01_l02: Flows: 40 Class: 1 Type: 1 13 16384 1472
 u01l02,u01l03: l01_s01-s01_l02-s01_l03: Flows: 1 Class: 1 Type: 1 13 16384 1472
```

- `@ <endpoint>` sets the source, as in a `.ufl` file.
- A request line is `<destinations>: <interior links>: Flows: <count> Class: <class> Type: <type> <rate> <latency_us> <payload>`.
- Interior links are link ids separated by `-`. An empty interior is written as an empty field between the two colons.
- The resulting path id is `ingress^sorted-interior^sorted-egress`.
- `Class` and `Type` are accepted and not stored.
- `<rate>` becomes the shaping rate `SR_Mbps`.
- Latency and payload use the same conversion as a `.ufl` file.
- `B_Ref` is 1600 bytes. Packet classification is the same fixed classification.
- On read, flow ids are assigned in order, starting at `1`. The file does not carry the ids, minimum rate, unit cost, or VLAN that the controller holds in memory.

A path that does not reach the declared destinations is rejected and reported. The other flows in the file are still loaded.

## 5. Operation

### 5.1 Starting the controller

From the repository root, after `check_dependencies.sh` has succeeded:

```bash
./start_daas_cc.sh
```

The script checks that `.venv/bin/python` exists, then runs `start_server.py`. Extra arguments are passed through:

```bash
./start_daas_cc.sh --config daas_cc/examples/poc-10G-config.json
```

The same entry point, using the virtual environment explicitly:

```bash
.venv/bin/python start_server.py
```

`start_server.py` options:


| Option                  | Meaning                                                                                                                 |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `--config`, `-c`        | Config file. Default `poc-10G-config.json`, resolved under `daas_cc/examples/`.                                         |
| `--reload`              | Restart the process when source files change. This builds the controller twice, so a large flow list is admitted twice. |
| `--no-reload`           | One process. This is the default.                                                                                       |
| `--fresh-imports`       | Drop cached `daas_cc` modules before start.                                                                             |
| `--clear-pycache`       | Delete `__pycache__` directories under `daas_cc/` before start.                                                         |
| `--dont-write-bytecode` | Set `PYTHONDONTWRITEBYTECODE` for this run.                                                                             |
| `--kill-port PORT`      | On Windows, try to stop a process listening on `PORT`.                                                                  |


The server listens on `0.0.0.0:8000`.

Startup loads the topology, the VLAN map, and any configured flow files before the API accepts requests. With `push_pss_on_startup` true, it then writes the PSS files. Wait for `Application startup complete` before calling the API.

Stop the server with Ctrl+C.

### 5.2 Web interface and API

There is no separate graphical application. The interface is the generated API page:

- API: `http://localhost:8000`
- Interactive documentation: `http://localhost:8000/docs`

That page lists every endpoint, the query parameters, and the request body, and it can send a call. The descriptions below are the operational meaning of each one.

**State**


| Method and path                       | What it does                                                                                   |
| ------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `GET /state`                          | Residual capacity per link and the flow count. Does not return every flow.                     |
| `GET /state/summary`                  | Flow counts by endpoint, total allocated rate, and utilization of every link.                  |
| `GET /state/full`                     | Residual capacity and every flow. Very slow or even inconclusive when many flows are admitted. |
| `GET /state/flows?limit=100&offset=0` | One page of flows.                                                                             |


**Flows**


| Method and path                    | What it does                                                                                                                                                                                                                                                                                                                           |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /flows/add?count=1`          | Admit one or more copies of a request. The body is a flow specification: `src_id`, `dst_id` (a list), `D_Max_ns`, `L_Max_bytes`, `B_Ref_bytes`, `R_Min_Mbps`, and optional `PCS_R`. One destination is unicast and is routed. Several destinations are multicast. `count` repeats the same request. HTTP 409 means none were admitted. |
| `GET /flows/all`                   | Admitted flows. Optional `limit` and `offset`. Above 1000 flows the call asks you to paginate instead of returning the whole table.                                                                                                                                                                                                    |
| `GET /flows/multicast-only`        | Admitted flows that have more than one destination.                                                                                                                                                                                                                                                                                    |
| `GET /flows/{df_id}`               | One flow by id.                                                                                                                                                                                                                                                                                                                        |
| `GET /flows/src_endpoint/{src_id}` | Flows whose source is that endpoint.                                                                                                                                                                                                                                                                                                   |
| `GET /flows/dst_endpoint/{dst_id}` | Flows that include that destination.                                                                                                                                                                                                                                                                                                   |
| `POST /flows/modify?df_id=<id>`    | Replace the specification of an admitted flow and admit it again. The body is the same flow specification as `POST /flows/add`. HTTP 409 means the change was not accepted.                                                                                                                                                            |
| `DELETE /flows/{df_id}`            | Remove one flow and release its bandwidth. HTTP 404 means the id is unknown.                                                                                                                                                                                                                                                           |


**Cost**


| Method and path                                          | What it does                                                                                                                                               |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /ref_unit_cost/config`                              | Current reference unit cost.                                                                                                                               |
| `POST /ref_unit_cost/modify?reference_unit_cost=<value>` | Change the reference unit cost in USD/s. The value must be at least 0. Flows already admitted keep their cost. Flows admitted afterward use the new value. |


**Links**


| Method and path        | What it does                                                                                          |
| ---------------------- | ----------------------------------------------------------------------------------------------------- |
| `GET /links/all`       | Capacity, deterministic capacity, used bandwidth, residual bandwidth, and utilization for every link. |
| `GET /links/{link_id}` | The same figures for one link. HTTP 404 means the link is unknown.                                    |


**Schedule**


| Method and path                                   | What it does                                                                                                                                                            |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /scheduler/config`                           | Current scheduling policy, slot duration, period, link rate, rate granularity, and the list of policies.                                                                |
| `POST /scheduler/modify?scheduling_policy=<name>` | Switch to `ras`, `flat`, or `plain_hierarchical`. A real change recomputes the schedule for every ingress and writes the PSS files. The same policy again does nothing. |


**PSS files**


| Method and path      | What it does                                                                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /pss/push`     | Recompute the schedule for every ingress and write both cfg files.                                                                                 |
| `POST /pss/push-new` | Write cfg files only for ingresses whose profile changed since the last write. If the controller is not tracking changes, it writes every ingress. |


**Flow table**


| Method and path                          | What it does                                                                                                                                                                                                                                                                  |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /flow_table/log?file_name=<name>`  | Write the admitted flows as a `.rfl` file. The default name is `routed-<topology-stem>-log.rfl` next to the topology file. A name that does not end in `.rfl` is given that suffix.                                                                                           |
| `POST /flow_table/load?file_name=<name>` | Read a `.rfl` file and install those routed flows. A relative name is resolved next to the topology file. The name must end in `.rfl`. Refused with HTTP 409 when flows are already present, unless `allow_load_log_with_existing_flows` is `true`. Does not write PSS files. |


**Debug**


| Method and path      | What it does                                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------------------------ |
| `GET /debug/version` | Reports whether the cost fields are present on the loaded flow model. Used to confirm which code is running. |


## 6. Output files

### 6.1 PSS instances

With `push_pss_on_startup` true, or after `POST /pss/push` or a scheduling-policy change, the controller writes one pair of files per ingress endpoint into the PSS output directory:

- `<endpoint>-TM.cfg`, the timeslot schedule
- `<endpoint>-STREAMS.cfg`, the stream table

The endpoint name is the ingress link id with the last `_segment` removed. `u01l01_l01` produces `u01l01-TM.cfg` and `u01l01-STREAMS.cfg`.

Both files are tab-separated. `*-STREAMS.cfg` has a `[CONFIG_STREAMS]` table whose columns are id, source IP, destination IP, rate in Mb/s, latency in microseconds, packet size, VLAN id, and VLAN priority. `*-TM.cfg` has `[CONFIG_TOPLVL]`, `[GBS_TIMESLOT_QUEUE_MAP]`, and `[GBS_SCHEDULING_RATE]`. The timeslot map assigns each slot to a queue. Queue `0` is the empty queue.

The write replaces the current pair. Older copies are not kept. An ingress with no flows still receives a pair.

A multicast flow with no VLAN is written in the stream file with VLAN `0x0` and destination address `224.0.0.1`. When a VLAN is assigned, the destination address is `224.0.0.<vlan mod 256>`, with a host of at least 1. A unicast destination address is the `ip_address` of the destination node.

### 6.2 The `.rfl` file

`POST /flow_table/log` writes the current population in the `.rfl` layout from section 4.5. Identical flows that share source, destinations, interior links, rate, latency, and payload become one line, and `Flows` is the count. `Class` and `Type` are written as `1`.

Loading that file restores the routed paths and the shaping rates. It assigns new flow ids and does not restore minimum rate, unit cost, or VLAN. Those values are applied again by the controller: the VLAN from the multicast map or the topology `paths` table, and the cost from the cost policy.

## 7. Other operating notes

- `DAAS_CONFIG_FILE` selects the config file when `--config` is not passed.
- An optional `.env` file in the repository root is loaded when python-dotenv is installed. The controller starts without it.
- Switching the 10 Gbps mix means editing `pre_unrouted_flows_file` or `pre_routed_flows_file` and starting again. A running controller does not reread the config file.
- `POST /flow_table/load` is the way to bring in another `.rfl` file without restarting, subject to `allow_load_log_with_existing_flows`.
- `GET /docs` is part of the server. `GET /state/full` and an unpaginated `GET /flows/all` are the calls that become large.

