# HVAC test: what to run on the IOC laptop

This laptop runs two things: the **EPICS soft IOC** (loads the supplied `db/hvac_test.db`) and the
**simulator** (`hvac_sim`, plays the field I/O and writes the values into the IOC). The monitoring
application runs on the other laptop and only reads. The full test plan is in the PLC_Monitoring repository:
`docs/v2/03-hvac-e2e-test.md`.

## Once

1. Allow incoming **UDP 5064** and **TCP 5064** (Windows firewall; network profile "Private"). Admin PowerShell:

   ```powershell
   New-NetFirewallRule -DisplayName "EPICS CA UDP" -Direction Inbound -Protocol UDP -LocalPort 5064 -Action Allow
   New-NetFirewallRule -DisplayName "EPICS CA TCP" -Direction Inbound -Protocol TCP -LocalPort 5064 -Action Allow
   ```

2. Python 3.9+ and, for the efficient writer, `pip install pyepics` (it normally brings its own `ca.dll`; if it cannot find one,
   set `PYEPICS_LIBCA` to `.../base-7.0.10/bin/windows-x64-mingw/ca.dll`). Without pyepics use `--writer caput`.

## Every time (Git Bash)

Terminal 1, the IOC:

```bash
export EPICS_BASE=/c/msys64/home/samee/EPICS/base-7.0.10
export PATH="$EPICS_BASE/bin/windows-x64-mingw:$PATH"
export EPICS_CA_SERVER_PORT=5064
cd /d/plc_simulator/ioc
softIoc -d db/hvac_test.db -d db/hvac_sim_support.db
#  if softIoc rejects a second -d:   softIoc hvac_ioc.cmd
```

At the `epics>` prompt: `dbl` lists 29 records; `dbgf HVAC:AHU1:SupplyAirTemp` shows the value (it is the initial 14.5 until the simulator writes).

Terminal 2, the simulator:

```bash
export EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO
cd /d/plc_simulator/ioc
python -m hvac_sim run --writer pyepics --scenario normal --interval 1
#  without pyepics:  python -m hvac_sim run --writer caput --caput "$EPICS_BASE/bin/windows-x64-mingw/caput.exe" --interval 2
```

It is fine to start it before the IOC: it reports that the PVs are not connected and keeps trying.

## During the test

```bash
cd /d/plc_simulator/ioc
python -m hvac_sim scenarios                  # the list
python -m hvac_sim set smoke_ahu1             # switch the running simulator to a fault
python -m hvac_sim set normal                 # back to normal
```

Look at the simulator log line `expected-alarms=…`: it states which native alarms the record limits imply for the current
values (a model, to compare with what the application shows; the real alarms come from the IOC).

Stop the IOC (Ctrl+C in terminal 1) and start it again to test connection loss: the simulator refills the records within 10 s.
Stop only the simulator to see `HVAC:SIM:SimulatorAlive` go to a MAJOR alarm (≈ 5 to 10 s) and the analog PVs go stale in the application.

## Check without the application

```bash
python -m unittest discover -s tests          # simulator tests, no EPICS needed
python -m hvac_sim run --writer dry-run --duration 5 --scenario smoke_ahu1 --speed 20   # prints what would be written
caget HVAC:AHU1:SupplyAirTemp HVAC:AHU1:SmokeDetected      # EPICS Base tools, if installed
```

Nothing on this laptop is part of the monitoring application, and the application never writes to this IOC.
