"""The simulator loop: catalog + model + scenario + writer."""
import json
import os
import time

from . import epics_alarm
from .catalog import Catalog
from .model import World, build_modules
from .scenarios import Runner, SCENARIOS


class Simulator:
    def __init__(self, db_files, writer, scenario="normal", seed=1, day_seconds=1200.0, start_hour=10.0,
                 refresh_s=10.0, control_file=None, log=print):
        self.catalog = Catalog.load(*db_files)
        self.world = World(seed=seed, day_seconds=day_seconds, start_hour=start_hour)
        self.modules, self.unmodelled = build_modules(self.catalog, self.world)
        self.runner = Runner(self.world, self.modules)
        self.runner.start(scenario)
        self.writer, self.log = writer, log
        self.refresh_s = refresh_s
        self.control_file = control_file
        self._control_mtime = 0.0
        self.last_sent = {}          # pv -> last value written
        self.last_full = -1e9        # simulated time of the last full refresh
        self.values = {}             # pv -> current value (what the IOC should hold)
        self.alarm_state = {}        # pv -> epics_alarm.AlarmState (for the expected-alarm report)
        self.expected = {}           # pv -> (status, severity) the record configuration implies
        # Points the .db has but no module can produce are left alone (they keep their initial VAL).
        produced = {m.name: set(m.POINTS) for m in self.modules}
        self.unsimulated = sorted(
            r.name for r in self.catalog
            if r.rtype not in ("calc", "calcout", "sub", "aSub")  # computed inside the IOC, never written
            and (r.equipment not in produced or r.point not in produced.get(r.equipment, ())))
        if self.unmodelled:
            log("no model for equipment %s: its PVs keep their initial values" % ", ".join(sorted(set(self.unmodelled))))
        if self.unsimulated:
            log("%d PV(s) have no simulated point and keep their initial value: %s" % (len(self.unsimulated), ", ".join(self.unsimulated[:6]) + (" ..." if len(self.unsimulated) > 6 else "")))

    # -- control -----------------------------------------------------------------------------
    def set_scenario(self, name):
        self.runner.start(name)
        self.log("scenario -> %s: %s" % (name, SCENARIOS[name].description))

    def _poll_control(self):
        if not self.control_file or not os.path.exists(self.control_file):
            return
        m = os.path.getmtime(self.control_file)
        if m == self._control_mtime:
            return
        self._control_mtime = m
        try:
            with open(self.control_file, encoding="utf-8") as fh:
                name = json.load(fh).get("scenario")
        except (OSError, ValueError):
            return
        if name and name != self.runner.name:
            try:
                self.set_scenario(name)
            except KeyError as e:
                self.log(str(e))

    # -- one step ----------------------------------------------------------------------------
    def step(self, dt):
        """Advance the plant by dt simulated seconds. Returns {pv: value} that were sent to the IOC."""
        self._poll_control()
        self.world.t += dt
        self.runner.tick()
        sent = {}
        full = self.world.t - self.last_full >= self.refresh_s
        if full:
            self.last_full = self.world.t
        for m in self.modules:
            for point, raw in m.step(dt).items():
                rec = m.records[point]
                v = self._shape(rec, raw)
                self.values[rec.name] = v
                st = self.alarm_state.setdefault(rec.name, epics_alarm.AlarmState())
                self.expected[rec.name] = epics_alarm.evaluate(rec, v, st)
                if full or self.last_sent.get(rec.name) != v:
                    if self.writer.put(rec.name, v) is not False:
                        self.last_sent[rec.name] = v
                        sent[rec.name] = v
        return sent

    @staticmethod
    def _shape(rec, raw):
        """Clamp to the display range and round to the record's precision (a sensor reports finite digits)."""
        if rec.rtype in ("stringin", "stringout"):
            return str(raw)
        if rec.is_binary or rec.is_enum:
            return int(raw)
        rng = rec.writable_range
        if rng:
            raw = max(rng[0], min(rng[1], raw))
        v = round(raw, rec.prec)
        return int(v) if rec.prec == 0 else v

    # -- reporting ------------------------------------------------------------------------------
    def expected_alarms(self):
        """{pv: (status, severity)} for PVs that the record configuration puts in alarm right now."""
        return {pv: sv for pv, sv in self.expected.items() if sv[1] > 0}

    def run(self, interval=1.0, speed=1.0, duration=None, status_every=10.0, sleep=time.sleep):
        t_end = None if duration is None else time.time() + duration
        last_status = 0.0
        self.log("simulator running: %d PVs in the catalog, %d simulated, interval %.2fs, speed x%g, writer %s, scenario %s"
                 % (len(self.catalog), len(self.catalog) - len(self.unsimulated), interval, speed, self.writer.name, self.runner.name))
        try:
            while t_end is None or time.time() < t_end:
                t0 = time.time()
                self.step(interval * speed)
                if time.time() - last_status >= status_every:
                    last_status = time.time()
                    alarms = self.expected_alarms()
                    self.log("t=%7.0fs hour=%4.1f scenario=%s expected-alarms=%s" % (
                        self.world.t, self.world.hour, self.runner.name,
                        ", ".join("%s:%s/%s" % (pv.split(":", 1)[-1], st, epics_alarm.SEV_NAME[sv]) for pv, (st, sv) in sorted(alarms.items())) or "none"))
                sleep(max(0.0, interval - (time.time() - t0)))
        except KeyboardInterrupt:
            self.log("stopped")
        finally:
            self.writer.close()
