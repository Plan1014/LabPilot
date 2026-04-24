"""PNA Service configuration.

All settings can be overridden via environment variables.
Defaults are suitable for local development.
"""

import os
from pathlib import Path

# Instrument connection
PNA_RESOURCE = os.getenv("PNA_RESOURCE", "USB::0xAAD::0x290::101334::INSTR")
PNA_VISA_TIMEOUT = int(os.getenv("PNA_VISA_TIMEOUT", "1500000"))
PNA_OPC_TIMEOUT = int(os.getenv("PNA_OPC_TIMEOUT", "800000"))

# Data output
PNA_DATA_DIR = Path(os.getenv("PNA_DATA_DIR", "data/PNA_data"))

# Default measurement parameters
PNA_DEFAULT_START_FREQ = int(os.getenv("PNA_DEFAULT_START_FREQ", "1"))
PNA_DEFAULT_STOP_FREQ = int(os.getenv("PNA_DEFAULT_STOP_FREQ", "100000"))
PNA_DEFAULT_FILENAME = os.getenv("PNA_DEFAULT_FILENAME", "trace_{timestamp}.csv")

# Service port
PNA_PORT = int(os.getenv("PNA_PORT", "8002"))