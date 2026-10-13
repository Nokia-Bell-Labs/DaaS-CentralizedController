# DaaS Centralized Controller

Determinism as a Service (DaaS) keeps latency bounds for selected flows in a packet network. It has two parts.

The **Routing-Aware Shaper (RAS)** runs on the forwarding nodes. Its open-source implementation is the DPDK application `tm10` in [DPDK-RoutingAwareShaper](https://github.com/Nokia-Bell-Labs/DPDK-RoutingAwareShaper). `tm10` classifies packets, queues them, and serves those queues from a periodic schedule.

This repository is the other part: the **DaaS Centralized Controller (DaaS-CC)**. It does not forward packets. It decides which flows are admitted, which path each flow takes, and which timeslot schedule each ingress should run. From that decision it writes the two files `tm10` loads for an endpoint:

- `<endpoint>-TM.cfg`, the periodic timeslot schedule
- `<endpoint>-STREAMS.cfg`, the streams to classify, shape, and map onto that schedule

One pair is produced for each ingress endpoint. The controller writes the files locally. Loading them into `tm10`, and building and running the shaper, are described in the RAS repository.

The publications cited there, *Lightweight Determinism in Large-Scale Networks* and *Routing-Aware Shaping for Feasible Multi-Domain Determinism*, describe the RAS and the DaaS design this controller implements.

## Usage

Install, configure, start, and operate the controller as described in the [user manual](docs/UserManual.md).
