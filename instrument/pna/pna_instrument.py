"""PNA Instrument control.

Wraps RsInstrument for phase noise analyzer operations.
"""

from RsInstrument import RsInstrument
import os
import csv
from typing import Optional
from pathlib import Path

from instrument.pna.config import (
    PNA_RESOURCE,
    PNA_VISA_TIMEOUT,
    PNA_OPC_TIMEOUT,
    PNA_DATA_DIR,
)


class PNAInstrument:
    """PNA Phase Noise Analyzer instrument control."""

    def __init__(
        self,
        resource: str = PNA_RESOURCE,
        visa_timeout: int = PNA_VISA_TIMEOUT,
        opc_timeout: int = PNA_OPC_TIMEOUT,
    ):
        self.resource = resource
        self.visa_timeout = visa_timeout
        self.opc_timeout = opc_timeout
        self._pna: Optional[RsInstrument] = None

    def connect(self):
        """Connect to PNA instrument with 3 retry attempts and exponential backoff."""
        retry_delays = [5, 10, 20]  # seconds
        last_error = None

        for attempt, delay in enumerate(retry_delays):
            try:
                self._pna = RsInstrument(
                    self.resource,
                    reset=False,
                    id_query=True,
                    options="SelectVisa='rs'"
                )
                idn_response = self._pna.query_str("*IDN?")
                print(f"PNA connected: {idn_response}")
                self._pna.instrument_status_checking = True
                self._pna.visa_timeout = self.visa_timeout
                self._pna.opc_timeout = self.opc_timeout
                return  # Success
            except Exception as e:
                last_error = e
                if self._pna:
                    try:
                        self._pna.close()
                    except Exception:
                        pass
                    self._pna = None
                if attempt < len(retry_delays) - 1:
                    print(f"PNA connection attempt {attempt + 1} failed, retrying in {delay}s...")
                    import time
                    time.sleep(delay)
                else:
                    print(f"PNA connection failed after {len(retry_delays)} attempts: {e}")

        # All retries exhausted
        raise ConnectionError(f"Failed to connect to PNA after {len(retry_delays)} attempts: {last_error}")

    def configure(self, start_freq: int, stop_freq: int):
        """Configure instrument for measurement."""
        self._pna.write_str_with_opc("SYSTEM:DISPLAY:UPDATE ON")
        self._pna.write_str_with_opc("INITiate:CONTinuous OFF")
        self._pna.write_str_with_opc(f"SENSe:FREQuency:STARt {start_freq}")
        self._pna.write_str_with_opc(f"SENSe:FREQuency:STOP {stop_freq}")
        self._pna.write_str_with_opc("SENSe:SWEep:XFACtor 10")

    def measure(self) -> list:
        """Perform measurement and return trace data."""
        self._pna.write_str("INITiate:IMMediate")
        opc_response = self._pna.query_str("*OPC?")
        print("PNA measurement complete")
        trace_data = self._pna.query_str("TRACe:DATA? TRACE1").split(",")
        return [float(x) for x in trace_data]

    def save(self, trace_data: list, csv_filename: str) -> Path:
        """Save trace data to CSV file."""
        PNA_DATA_DIR.mkdir(parents=True, exist_ok=True)
        save_path = PNA_DATA_DIR / csv_filename

        freq_points = trace_data[::2]
        power_points = trace_data[1::2]

        with open(save_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Frequency_Hz", "Power_dBm"])
            for freq, power in zip(freq_points, power_points):
                writer.writerow([freq, power])

        print(f"Saved to {save_path}")
        return save_path

    def disconnect(self):
        """Close instrument connection."""
        if self._pna:
            self._pna.close()
            self._pna = None
            print("PNA disconnected")

    def run(self, start_freq: int, stop_freq: int, csv_filename: str) -> Path:
        """Execute full measurement and return CSV path."""
        self.connect()
        self.configure(start_freq, stop_freq)
        trace_data = self.measure()
        csv_path = self.save(trace_data, csv_filename)
        self.disconnect()
        return csv_path


def run_measurement(task_id: str, start_freq: int, stop_freq: int, csv_filename: str, callback=None):
    """Run measurement in background thread. Calls callback(task_id, status, result) when done."""
    try:
        pna = PNAInstrument()
        csv_path = pna.run(start_freq, stop_freq, csv_filename)

        freq_count = len(csv_path.read_text().split("\n")) - 2  # approximate

        result = {
            "status": "success",
            "csv_path": str(csv_path),
            "trace_points": freq_count,
        }

        if callback:
            callback(task_id, "completed", result)
        return result

    except Exception as e:
        print(f"PNA measurement error: {e}")
        result = {"status": "failed", "error": str(e)}
        if callback:
            callback(task_id, "failed", result)
        return result