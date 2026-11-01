# Coal power plant simulator (1000 PVs)

One 300 MW pulverised-coal unit as an EPICS soft IOC with 1000 records (830 analog `ai`, 170 binary `bi`), and a simulator
(`coal_sim`) that feeds them over Channel Access. Same idea as `hvac_sim`: the IOC evaluates the alarm limits, the
monitoring application only reads.

| Section | PVs | Examples |
|---|---|---|
| Boiler | 210 | drum pressure and level, main steam, furnace pressure, O2, 190 tube-metal thermocouples |
| Air and Flue Gas | 160 | FD / PA / ID fans, air preheaters, 8 ESP fields, FGD pumps and absorber, stack emissions |
| Pulverisers | 138 | mills A-F: motor current, outlet temperature, feed rate, bearings, vibration, flames |
| Feedwater | 107 | boiler feed pumps A-C, condensate pumps, HP and LP heaters, deaerator |
| Condenser and Cooling | 85 | vacuum, hotwell, CW pumps, 6 cooling tower fans, vacuum pumps |
| Turbine | 71 | speed, 8 bearings (metal temp, vibration X/Y), thrust pads, expansions, EHC and lube oil, valves |
| Ash Handling, Coal Handling | 57 + 56 | conveyors, crushers, bunkers, bottom ash, 12 fly-ash hoppers |
| Electrical, Generator | 43 + 38 | MW, frequency, stator and gas temperatures, hydrogen, buses, transformers, DC system |
| Water and Utilities | 35 | boiler water chemistry, DM plant, instrument air, fire water |

PV names are `COAL:<Equipment>:<Point>`, for example `COAL:MillA:MotorCurrent`, `COAL:Boiler:MainSteamTemp`.

## What makes it behave like a plant

* **Random feed time per PV: 0.25 to 5 s.** Every point has its own delay, drawn again at random for each update. It is never
  outside 0.25 to 5 s.
* **The feed time changes over time.** Each point's typical delay is drawn again every 45 to 240 s (a fast point becomes slow
  later), and a slow plant-wide wave speeds all points up and down by up to 30 %.
  With 1000 PVs this is about 600 to 700 writes per second.
* **One unit load drives everything.** The load runs a random programme of ramps (40-150 s) and plateaus (50 to 100 %). Flows,
  currents, pressures and temperatures follow it with their own lag, noise and slow drift.
* **Machines start, stop and trip.** Mill E starts above 55 % load and mill F above 80 %, standby pumps and fans come in at
  higher load, and machines trip now and then (`Tripped` goes to MAJOR, `Running` to STOPPED, the current falls to 0,
  a temperature falls to ambient) and restart after 1 to 3 minutes.
* **Faults.** About 30 times an hour a random running point is driven to its HIGH, HIHI, LOW or LOLO limit for 30 to 150 s,
  so the IOC raises real MINOR and MAJOR alarms. In normal running (`--fault-rate 0`) no analog point reaches a limit.
* Binary switches (pull cord, rapper fault, high level, Buchholz relay ...) switch on at random and fall back.

## Once

```bash
cd /d/plc_simulator/ioc
python -m coal_sim generate-db          # only if you change coal_sim/plant.py; db/coal_plant.db is already generated
python -m coal_sim export-inventory     # generated/coal_inventory.csv, for the monitoring application's CSV import
pip install pyepics                     # the caput writer starts one process per value and cannot keep up with 1000 PVs
```

Firewall and addresses are the same as in `HVAC_IOC_LAPTOP.md`.

## Every time

Terminal 1, the IOC (do not load it together with the HVAC database if you also need `HVAC:` PVs separately; they can run in one IOC, the names do not clash):

```bash
export EPICS_BASE=/c/msys64/home/samee/EPICS/base-7.0.10
export PATH="$EPICS_BASE/bin/windows-x64-mingw:$PATH"
export EPICS_CA_SERVER_PORT=5064
cd /d/plc_simulator/ioc
softIoc -d db/coal_plant.db
```

Terminal 2, the simulator:

```bash
export EPICS_CA_ADDR_LIST=127.0.0.1 EPICS_CA_AUTO_ADDR_LIST=NO
cd /d/plc_simulator/ioc
python -m coal_sim run
```

It connects all 1000 channels first (it reports how many connected), then prints a status line every 10 s: unit load, writes
per second, the smallest / mean / largest feed delay of that period, machines running, and the alarms the record limits imply.

| Command | |
|---|---|
| `python -m coal_sim set-load 95` | pin the unit load at 95 % (for example to start mill F); `set-load auto` gives it back |
| `python -m coal_sim run --fault-rate 0` | no random limit excursions |
| `python -m coal_sim run --fault-rate 120` | more alarms |
| `python -m coal_sim run --writer dry-run --duration 30` | no IOC needed; prints the status lines only |
| `python -m coal_sim run --seed 7` | another (repeatable) sequence |
| `python -m coal_sim list` | PV count per plant section |

Importing into the monitoring application: use `generated/coal_inventory.csv`. Its `system` column is the plant section and
`equipment` the machine; units use the Unit Catalog symbols (`°C`, `m³/hr`, `µm`). Units that are not in the Unit Catalog
(`kg/hr` and `bar` are, but `ppb`, `kN`, `mg/m3`, `kg/m3`, `1/min`) are rejected per row until an administrator adds them in the
Unit Catalog tab, or you clear them in the CSV. pH, conductivity and power factor have no unit.

## Tests

```bash
python -m unittest tests.test_coal_sim      # about 40 s, no EPICS needed (virtual clock)
```

They check: 1000 unique, valid-length names; consistent ranges and limits; the `.db` parses; every point is fed; every delay
is within 0.25 to 5 s, spread over the whole range and different per point and per period; values stay in range; the
load drives the values; a stopped machine falls to its off value; faults reach the limits; no alarm without faults; same seed,
same values.

## Not verified

The `.db` and the Channel Access writes have **not** been run against a real `softIoc` here (no EPICS Base on the development
machine). The first start on the IOC laptop may show a field the installed EPICS Base reads differently; the file uses only the
fields of `db/hvac_test.db`. The load on an old IOC laptop (about 650 puts per second) is also untested; if the status line shows
send failures or the loop falls behind, use `--count 500`.
