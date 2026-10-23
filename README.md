# plc_simulator

A Modbus TCP server that behaves like a small PLC. It is the test data source for
[PLC_Monitoring](https://github.com/sameermurtaza11/PLC_Monitoring), so the monitoring app can be
developed and demoed without real hardware.

Each simulator process is one PLC. Run several to get several PLCs.

## Memory map (identical on every simulated PLC)

| Type | Addresses | Modbus function | Size | Signal |
|---|---|---|---|---|
| Discrete Inputs (DI01–DI16) | 10001 – 10016 | FC02 | 1 bit | on/off square waves, 4–120 s periods; DI16 is a random "trip" (about every 2 min, lasts 3–10 s) |
| Input Registers (AI01–AI10) | 30001 – 30010 | FC04 | 16 bit, raw 0–65535 | slow sine waves (60–600 s periods) plus a little noise |

Each PLC gets its own phase, derived from `host:port` (or `--seed`), so two PLCs never show identical
values. The same `host:port` always gives the same pattern. Holding registers (4xxxx) are not simulated.

## Requirements

- Python 3.9 or newer
- `pyModbusTCP`

```powershell
pip install pyModbusTCP
```

## Run one PLC

```powershell
python plc_simulator.py --host 127.0.0.1 --port 502
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | IP to listen on. Use `0.0.0.0` to accept connections from other computers. |
| `--port` | `5020` | TCP port. Modbus standard is `502`. |
| `--interval` | `0.1` | Seconds between value updates. |
| `--seed` | from host:port | Fix the random pattern. |
| `--quiet` | off | No periodic value printout. |

Stop with Ctrl+C.

## Run three PLCs on one Windows computer

Every `127.x.x.x` address is local loopback on Windows and Linux, so one computer can host several
PLCs with different IPs and no extra network adapters. `plc_service.ps1` starts all three:

```powershell
.\plc_service.ps1 start     # PLC-1 127.0.0.1, PLC-2 127.0.0.2, PLC-3 127.0.0.3 (port 502)
.\plc_service.ps1 status
.\plc_service.ps1 stop
.\plc_service.ps1 restart
```

Logs go to `logs\<PLC name>.log` and `.err`. If PowerShell blocks the script:

```powershell
powershell -ExecutionPolicy Bypass -File .\plc_service.ps1 start
```

To change the PLC list, edit `$Plcs` at the top of the script. Names, IPs and ports must match
`plc_metadata` in the monitoring app's database.

## Run on a different computer than the monitoring app

The `127.x.x.x` addresses only work on the same machine. Over a network, give each PLC its own port
instead:

1. Find this computer's IP with `ipconfig` (for example `192.168.1.50`).
2. Allow the ports through the firewall (admin PowerShell):
   ```powershell
   New-NetFirewallRule -DisplayName "PLC Sim" -Direction Inbound -Protocol TCP -LocalPort 502-504 -Action Allow
   ```
3. Start one simulator per PLC, each in its own window:
   ```powershell
   python plc_simulator.py --host 0.0.0.0 --port 502 --interval 0.1 --quiet
   python plc_simulator.py --host 0.0.0.0 --port 503 --interval 0.1 --quiet
   python plc_simulator.py --host 0.0.0.0 --port 504 --interval 0.1 --quiet
   ```
4. From the monitoring computer, check the connection:
   ```powershell
   Test-NetConnection 192.168.1.50 -Port 502
   ```
   `TcpTestSucceeded : True` means it works.
5. In the monitoring app's `/config` page, set each PLC's IP to `192.168.1.50` and its port to
   502, 503 or 504, with Unit ID 1.

## Troubleshooting

- **Connection refused:** the simulator is not running, or the IP or port differs from the PLC's row in
  the monitoring app.
- **Works locally but not from another computer:** the simulator was started with `--host 127.0.0.1`.
  Use `0.0.0.0`, and check the firewall rule.
- **`ModuleNotFoundError: pyModbusTCP`:** run `pip install pyModbusTCP`.
- **Port already in use:** another simulator or Modbus server already holds that IP and port.
- **Simulators started by `plc_service.ps1` disappear:** read `logs\<PLC name>.err`. Starting the script
  from a normal PowerShell window rather than a tool-launched shell avoids processes being cleaned up
  with the parent.

## v2: EPICS test IOC

For the PLC_Monitoring v2 (EPICS Channel Access) there are test IOC databases and a 3000-record
generator in [`ioc/`](ioc/README.md). The Modbus simulator above remains as the v1 reference.
