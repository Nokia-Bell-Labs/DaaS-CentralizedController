from pydantic import BaseModel, Field
from typing import Literal, Optional
from pathlib import Path
import os
import json
import sys

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Get the project root directory (parent of daas_cc)
_PROJECT_ROOT = Path(__file__).parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"

if load_dotenv is not None:
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE)
        print(f"[OK] Loaded environment variables from {_ENV_FILE}")
    else:
        print(f"[INFO] Optional .env file not found at {_ENV_FILE}. Continuing with the current shell environment.")
else:
    print("[INFO] python-dotenv not installed. Environment variables will not be loaded from .env file.")
    print("  Install with: pip install python-dotenv")

# Get the directory containing this config file
_CONFIG_DIR = Path(__file__).parent
_EXAMPLES_DIR = _CONFIG_DIR / "examples"

# Default configuration file
_DEFAULT_CONFIG_FILE = "poc-10G-config.json"


def load_config_from_json(config_file: Optional[str] = None) -> dict:
    """
    Load configuration from JSON file.

    Args:
        config_file: Path to config file. If relative, searched in examples directory.
                    If None, uses default config file.

    Returns:
        Dictionary with configuration values
    """
    if config_file is None:
        config_file = _DEFAULT_CONFIG_FILE

    config_path = Path(config_file)

    # If relative path, look in examples directory
    if not config_path.is_absolute():
        config_path = _EXAMPLES_DIR / config_file

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    print(f"[OK] Loading configuration from {config_path}")

    with open(config_path, 'r') as f:
        config_data = json.load(f)

    # Resolve relative paths in the configuration
    # topology_file and flow files are relative to examples directory
    if 'topology_file' in config_data and config_data['topology_file']:
        topo_path = Path(config_data['topology_file'])
        if not topo_path.is_absolute():
            config_data['topology_file'] = str(_EXAMPLES_DIR / config_data['topology_file'])

    if 'pre_routed_flows_file' in config_data and config_data['pre_routed_flows_file']:
        flows_path = Path(config_data['pre_routed_flows_file'])
        if not flows_path.is_absolute():
            config_data['pre_routed_flows_file'] = str(_EXAMPLES_DIR / config_data['pre_routed_flows_file'])

    if 'pre_unrouted_flows_file' in config_data and config_data['pre_unrouted_flows_file']:
        flows_path = Path(config_data['pre_unrouted_flows_file'])
        if not flows_path.is_absolute():
            config_data['pre_unrouted_flows_file'] = str(_EXAMPLES_DIR / config_data['pre_unrouted_flows_file'])

    if 'flow_table_log_file' in config_data and config_data['flow_table_log_file']:
        log_path = Path(config_data['flow_table_log_file'])
        if not log_path.is_absolute():
            config_data['flow_table_log_file'] = str(_EXAMPLES_DIR / config_data['flow_table_log_file'])

    if 'multicast_vlan_mapping_file' in config_data and config_data['multicast_vlan_mapping_file']:
        mapping_path = Path(config_data['multicast_vlan_mapping_file'])
        if not mapping_path.is_absolute():
            config_data['multicast_vlan_mapping_file'] = str(_EXAMPLES_DIR / config_data['multicast_vlan_mapping_file'])

    # Compute pss_period_slots from pss_link_rate and pss_rate_granularity
    if 'pss_link_rate' in config_data and 'pss_rate_granularity' in config_data:
        config_data['pss_period_slots'] = config_data['pss_link_rate'] // config_data['pss_rate_granularity']

    return config_data


class ControllerConfig(BaseModel):
    # File paths
    topology_file: str
    pre_routed_flows_file: Optional[str] = None
    pre_unrouted_flows_file: Optional[str] = None
    flow_table_log_file: Optional[str] = None
    # Multicast path -> VLAN map. Unicast VLANs stay in the topology paths table.
    # A relative path is resolved under daas_cc/examples.
    multicast_vlan_mapping_file: Optional[str] = None
    state_dir: str = "state"
    # Directory for *-TM.cfg and *-STREAMS.cfg. This is the only place the
    # directory is configured. A relative path is resolved from the process
    # working directory.
    pss_output_dir: str = "pss-output"

    # Flow table log options
    allow_load_log_with_existing_flows: bool = False

    # Capacity configuration
    deterministic_capacity_fraction: float = 1.0

    # Timeslot and PSS configuration
    slot_duration_s: float = Field(0.000000512, description="Timeslot duration T (s).")
    pss_link_rate: int = Field(25000, description="Reference link rate (Mb/s)")
    pss_rate_granularity: int = Field(1, description="Rate allocation granularity (Mb/s)")
    pss_period_slots: int = Field(25000, description="PSS period in slots (computed from link rate / granularity)")

    # PSS startup behavior
    push_pss_on_startup: bool = Field(True, description="Compute PSS on startup and write *-TM.cfg and *-STREAMS.cfg")

    # Progress reporting during unrouted flow loading
    flow_routing_progress_interval_s: float = Field(5.0, description="Interval (s) between progress printouts while routing unrouted flows. Set to 0 to disable.")

    # Scheduling policy configuration
    scheduling_policy: Literal["ras", "flat", "plain_hierarchical"] = "ras"

    # Cost policy configuration
    cost_policy: Literal["manual_cost", "congestion_cost"] = "manual_cost"
    reference_unit_cost: float = Field(0.0, description="Reference unit cost for flows (USD/s), used as starting point for cost computation")

    # Routing policy configuration
    routing_policy: Literal[
        "shortest_hops",
        "shortest_latency",
        "largest_residual_bandwidth",
        "largest_residual_bandwidth_minimum_tree",
    ] = "largest_residual_bandwidth"
    routing_max_path_num: int = 8
    routing_max_hop_num: int = 4
    routing_max_latency_num_us: float = 1000000.0

    # Admission policy configuration
    admission_mode: Literal["exact", "conservative"] = "exact"
    bandwidth_overhead: float = Field(0.0, ge=0.0, description="Bandwidth reservation overhead as a percentage (e.g. 5 for 5%). 0 means no overhead.")
    conservative_cac_max_path_hops: int = 4
    conservative_cac_max_link_latency_s: float = 0.000010

    @classmethod
    def from_json_file(cls, config_file: Optional[str] = None) -> 'ControllerConfig':
        """
        Create ControllerConfig from JSON file.

        Args:
            config_file: Path to JSON config file. If None, uses default.

        Returns:
            ControllerConfig instance
        """
        config_data = load_config_from_json(config_file)
        return cls(**config_data)


# Global function to get configuration file from command line or environment
def get_config_file_path() -> Optional[str]:
    """
    Get configuration file path from command line arguments or environment variable.

    Priority:
    1. Command line argument --config or -c
    2. Environment variable DAAS_CONFIG_FILE
    3. None (uses default)

    Returns:
        Configuration file path or None for default
    """
    # Check command line arguments
    for i, arg in enumerate(sys.argv):
        if arg in ['--config', '-c'] and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith('--config='):
            return arg.split('=', 1)[1]

    # Check environment variable
    env_config = os.environ.get('DAAS_CONFIG_FILE')
    if env_config:
        return env_config

    # Use default
    return None

