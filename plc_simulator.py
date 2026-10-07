"""
PLC simulator — Modbus TCP server that behaves like a small PLC.

Memory map (same on every simulated PLC):

    Discrete Inputs   10001 - 10016   DI01 - DI16   1 bit  (FC02)
    Input Registers   30001 - 30010   AI01 - AI10   16 bit (FC04), raw 0 - 65535

Signals:
    AI  slow sine waves (different period per channel) + small noise,
        so trends look like real process values.
    DI  on/off signals, each with its own period (pumps running,
        valves open, level switches ...). DI16 is a random "trip" that
        comes on now and then.

Every PLC instance gets its own phase (derived from --host/--port or
--seed), so PLC-1 and PLC-2 do NOT show identical values.

Run one process per simulated PLC:

    python plc_simulator.py --host 127.0.0.1 --port 502     # PLC-1
    python plc_simulator.py --host 127.0.0.2 --port 502     # PLC-2
    python plc_simulator.py --host 127.0.0.3 --port 502     # PLC-3

On Windows and Linux every 127.x.x.x address is a local loopback address,
so this gives several PLCs with DIFFERENT IPs on one computer.
(plc_service.ps1 starts/stops all of them.)
"""

import argparse
import logging
import math
import random
import time
import zlib

from pyModbusTCP.server import ModbusServer

# ---------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------
PLC_HOST = "127.0.0.1"
PLC_PORT = 5020
UPDATE_INTERVAL = 0.1  # seconds between value updates

AI_COUNT = 10
DI_COUNT = 16
RAW_MAX = 65535

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")


class PLCSimulator:

    def __init__(self, host, port, seed=None):
        self.host = host
        self.port = port

        # Stable per-PLC randomness: same host:port → same behaviour every run.
        self.rng = random.Random(seed if seed is not None else zlib.crc32(f"{host}:{port}".encode()))

        # AI: period 60 … 600 s, random phase, centre 30 … 70 % of range.
        self.ai_period = [self.rng.uniform(60, 600) for _ in range(AI_COUNT)]
        self.ai_phase = [self.rng.uniform(0, 2 * math.pi) for _ in range(AI_COUNT)]
        self.ai_centre = [self.rng.uniform(0.3, 0.7) for _ in range(AI_COUNT)]
        self.ai_amp = [self.rng.uniform(0.15, 0.28) for _ in range(AI_COUNT)]

        # DI: square waves with periods 4 … 120 s and random duty/phase.
        self.di_period = [self.rng.uniform(4, 120) for _ in range(DI_COUNT)]
        self.di_duty = [self.rng.uniform(0.2, 0.8) for _ in range(DI_COUNT)]
        self.di_phase = [self.rng.uniform(0, 1) for _ in range(DI_COUNT)]
        self.trip_until = 0.0

        self.ai = [0] * AI_COUNT
        self.di = [False] * DI_COUNT

        self.server = ModbusServer(host=self.host, port=self.port, no_block=True)

    def generate_values(self, t):
        for i in range(AI_COUNT):
            x = self.ai_centre[i] + self.ai_amp[i] * math.sin(2 * math.pi * t / self.ai_period[i] + self.ai_phase[i])
            x += self.rng.gauss(0, 0.004)  # sensor noise
            self.ai[i] = max(0, min(RAW_MAX, int(round(x * RAW_MAX))))

        for i in range(DI_COUNT - 1):
            pos = (t / self.di_period[i] + self.di_phase[i]) % 1.0
            self.di[i] = pos < self.di_duty[i]

        # DI16: random trip, roughly once every ~2 minutes, lasting 3-10 s.
        if t >= self.trip_until and self.rng.random() < UPDATE_INTERVAL / 120:
            self.trip_until = t + self.rng.uniform(3, 10)
        self.di[DI_COUNT - 1] = t < self.trip_until

    def update_modbus_memory(self):
        # Offset 0 = 30001 / 10001 (pyModbusTCP uses zero-based offsets).
        self.server.data_bank.set_input_registers(0, self.ai)
        self.server.data_bank.set_discrete_inputs(0, self.di)

    def run(self, quiet):
        print(f"PLC simulator on {self.host}:{self.port}")
        print(f"  Discrete inputs : 10001 - {10000 + DI_COUNT}")
        print(f"  Input registers : 30001 - {30000 + AI_COUNT}  (raw 0 - {RAW_MAX})")
        print(f"  Update interval : {UPDATE_INTERVAL} s   —  Ctrl+C to stop")

        self.server.start()
        start = time.time()
        last_log = 0.0
        try:
            while True:
                t = time.time() - start
                self.generate_values(time.time())  # wall clock: phases continue across restarts
                self.update_modbus_memory()
                if not quiet and t - last_log >= 5:
                    last_log = t
                    di_bits = "".join("1" if b else "0" for b in self.di)
                    logging.info("AI %s | DI %s", self.ai[:4], di_bits)
                time.sleep(UPDATE_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopping PLC simulator...")
        finally:
            self.server.stop()
            print("PLC simulator stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Modbus TCP PLC simulator (DI + AI)")
    parser.add_argument("--host", default=PLC_HOST, help="IP to listen on, e.g. 127.0.0.2")
    parser.add_argument("--port", type=int, default=PLC_PORT)
    parser.add_argument("--interval", type=float, default=UPDATE_INTERVAL,
                        help="seconds between value updates")
    parser.add_argument("--seed", type=int, default=None,
                        help="fix the random pattern (default: derived from host:port)")
    parser.add_argument("--quiet", action="store_true", help="no periodic value printout")
    args = parser.parse_args()

    UPDATE_INTERVAL = args.interval
    PLCSimulator(args.host, args.port, args.seed).run(args.quiet)
