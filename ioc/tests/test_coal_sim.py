"""Tests of the coal plant simulator. No EPICS needed: a fake clock and a recording writer stand in for the IOC."""
import collections
import os
import statistics
import tempfile
import unittest

from coal_sim.dbgen import db_text, write_inventory
from coal_sim.engine import RATE_MAX, RATE_MIN, Engine, Simulator
from coal_sim.plant import build
from hvac_sim.catalog import Catalog
from hvac_sim.writers import Writer


class Recorder(Writer):
    name = "recorder"

    def __init__(self):
        self.clock = lambda: 0.0
        self.log = collections.defaultdict(list)   # pv -> [(t, value)]

    def put(self, pv, value):
        self.log[pv].append((self.clock(), value))
        return True


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.001)


def run_virtual(seconds, count=1000, seed=1, **kw):
    clk = FakeClock()
    rec = Recorder()
    rec.clock = clk
    sim = Simulator(build(count), rec, seed=seed, log=lambda *_: None, **kw)
    sim.run(duration=seconds, clock=clk, sleep=clk.sleep)
    return sim, rec


class PlantTests(unittest.TestCase):
    def test_default_count_is_1000_and_names_are_valid(self):
        plant = build(1000)
        self.assertEqual(len(plant.points), 1000)
        names = [p.name for p in plant.points]
        self.assertEqual(len(set(names)), 1000)
        for p in plant.points:
            self.assertLessEqual(len(p.name), 60, p.name)      # EPICS record name limit
            self.assertLessEqual(len(p.desc), 40, p.name)      # DESC is 40 characters
            self.assertLessEqual(len(p.egu), 15, p.name)
            self.assertTrue(p.name.startswith("COAL:") and p.name.count(":") == 2, p.name)
            self.assertLessEqual(len(p.znam), 25)
            self.assertLessEqual(len(p.onam), 25)

    def test_smaller_counts(self):
        for n in (1, 50, 777, 1000, 1200):
            self.assertEqual(len(build(n).points), n)

    def test_ranges_and_limits_are_consistent(self):
        for p in build(1000).points:
            if p.rtype != "ai":
                continue
            self.assertLess(p.lopr, p.hopr, p.name)
            self.assertTrue(p.lopr <= p.initial <= p.hopr, p.name)
            for v in (p.nom, p.idle):
                self.assertTrue(p.lopr <= v <= p.hopr, "%s %s" % (p.name, v))
            lim = p.lim
            self.assertLess(lim["HIGH"], lim["HIHI"], p.name)
            self.assertGreater(lim["HIGH"], p.nom, p.name)                 # normal operation raises no alarm
            self.assertLessEqual(lim["HIHI"], p.hopr, p.name)
            if "LOW" in lim:
                self.assertGreater(lim["LOW"], lim["LOLO"], p.name)
                self.assertLess(lim["LOW"], min(p.idle, p.nom), p.name)
                self.assertGreaterEqual(lim["LOLO"], p.lopr, p.name)

    def test_db_is_parsed_by_the_catalog(self):
        plant = build(1000)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "coal.db")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(db_text(plant))
            cat = Catalog.load(path)
        self.assertEqual(len(cat), 1000)
        self.assertEqual({r.rtype for r in cat}, {"ai", "bi"})
        r = cat["COAL:Boiler:MainSteamTemp"]
        self.assertEqual(r.egu, "degC")
        self.assertEqual(r.num("HHSV") is None, True)
        self.assertEqual(r.fields["HHSV"], "MAJOR")

    def test_inventory_uses_catalog_units(self):
        plant = build(1000)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inv.csv")
            write_inventory(plant, path)
            with open(path, encoding="utf-8", newline="") as fh:
                rows = fh.read().splitlines()
        self.assertEqual(len(rows), 1001)
        self.assertIn("°C", "\n".join(rows))
        self.assertNotIn("degC", "\n".join(rows))
        self.assertGreaterEqual(len({r.split(",")[2] for r in rows[1:]}), 8)    # several plant sections


class FeedTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim, cls.rec = run_virtual(400.0)

    def intervals(self):
        out = {}
        for pv, log in self.rec.log.items():
            ts = [t for t, _ in log]
            out[pv] = [b - a for a, b in zip(ts, ts[1:])]
        return out

    def test_every_point_is_fed(self):
        self.assertEqual(len(self.rec.log), 1000)

    def test_scheduled_delays_stay_between_a_quarter_and_five_seconds(self):
        ds = []
        eng = Engine(build(1000), seed=3, log=lambda *_: None)
        for now in range(0, 3000, 7):
            for s in eng.ps[::13]:
                ds.append(eng.next_delay(s, float(now)))
        self.assertGreaterEqual(min(ds), RATE_MIN)
        self.assertLessEqual(max(ds), RATE_MAX)
        self.assertLess(min(ds), 0.5)       # the fast end is really used
        self.assertGreater(max(ds), 4.0)    # and so is the slow end

    def test_analog_points_are_fed_at_random_times_inside_the_range(self):
        by_pv = self.intervals()
        analog = [p.name for p in self.sim.engine.plant.points if p.rtype == "ai"]
        tol = 0.06                           # the loop looks at the heap every 50 ms
        all_iv = []
        for pv in analog:
            iv = by_pv[pv][1:]               # the first push is spread over the first 3 s
            all_iv += iv
            self.assertGreaterEqual(min(iv), RATE_MIN - 1e-9, pv)
            self.assertLessEqual(max(iv), RATE_MAX + tol, pv)
        self.assertGreater(statistics.pstdev(all_iv), 0.8)            # genuinely spread, not one fixed period
        self.assertLess(min(all_iv), 0.4)
        self.assertGreater(max(all_iv), 4.5)

    def test_feed_rate_of_a_point_changes_over_time(self):
        by_pv = self.intervals()
        changed = 0
        analog = [p.name for p in self.sim.engine.plant.points if p.rtype == "ai"]
        for pv in analog[:300]:
            log = self.rec.log[pv]
            ts = [t for t, _ in log]
            first = [b - a for a, b in zip(ts, ts[1:]) if b < 150]
            last = [b - a for a, b in zip(ts, ts[1:]) if a > 250]
            if first and last and abs(statistics.mean(first) - statistics.mean(last)) > 0.5:
                changed += 1
        self.assertGreater(changed, 40, "a point's typical feed time should drift from one period to another")

    def test_total_write_rate_is_sane(self):
        rate = self.sim.writes / 400.0
        self.assertGreater(rate, 250)
        self.assertLess(rate, 800)


class ValueTests(unittest.TestCase):
    def test_values_stay_in_range_and_have_the_right_type(self):
        sim, rec = run_virtual(600.0, seed=5)
        for p in sim.engine.plant.points:
            for _, v in rec.log[p.name]:
                if p.rtype == "bi":
                    self.assertIn(v, (0, 1), p.name)
                else:
                    self.assertTrue(p.lopr <= v <= p.hopr, "%s=%s" % (p.name, v))
                    if p.prec == 0:
                        self.assertIsInstance(v, int)

    def test_values_move_and_follow_unit_load(self):
        sim, rec = run_virtual(30.0, seed=2, fault_rate_h=0)
        eng = sim.engine
        eng.pinned = 1.0
        for i in range(100):
            eng.tick(1000.0 + i)
        mw = [s for s in eng.ps if s.p.name == "COAL:Generator:ActivePower"][0]
        hi = [eng.compute(mw, 2000.0 + i) for i in range(40)][-1]
        eng.pinned = 0.5
        for i in range(10):
            eng.tick(2500.0 + i)
        lo = [eng.compute(mw, 3000.0 + i) for i in range(80)][-1]
        self.assertGreater(hi, 280)
        self.assertLess(lo, 245)

    def test_stopped_machine_goes_to_its_off_value(self):
        eng = Engine(build(1000), seed=1, fault_rate_h=0, log=lambda *_: None)
        eng.pinned = 0.5                                    # mill F runs from 80 % load
        for i in range(400):
            eng.tick(i * 0.5)
        run = [s for s in eng.ps if s.p.name == "COAL:MillF:Running"][0]
        cur = [s for s in eng.ps if s.p.name == "COAL:MillF:MotorCurrent"][0]
        self.assertEqual(eng.compute(run, 300.0), 0)
        for i in range(60):
            v = eng.compute(cur, 300.0 + i * 2)
        self.assertLess(v, 5)
        eng.pinned = 0.95
        for i in range(400):
            eng.tick(300.0 + i * 0.5)
        self.assertEqual(eng.compute(run, 600.0), 1)

    def test_faults_reach_the_alarm_limits(self):
        sim, rec = run_virtual(600.0, seed=7, fault_rate_h=300.0)
        self.assertGreater(len(sim.engine.events), 10)
        hit = 0
        for p in sim.engine.plant.points:
            if p.rtype == "ai" and "HIGH" in p.lim:
                if any(v >= p.lim["HIGH"] for _, v in rec.log[p.name]):
                    hit += 1
        self.assertGreater(hit, 5)

    def test_no_faults_means_no_analog_alarm_in_normal_running(self):
        sim, rec = run_virtual(600.0, seed=11, fault_rate_h=0)
        bad = []
        for p in sim.engine.plant.points:
            if p.rtype != "ai":
                continue
            for key in ("HIGH", "LOW"):
                if key in p.lim and any((v >= p.lim[key]) if key == "HIGH" else (v <= p.lim[key]) for _, v in rec.log[p.name]):
                    bad.append(p.name)
                    break
        self.assertEqual(bad, [])

    def test_same_seed_gives_the_same_values(self):
        _, a = run_virtual(120.0, count=200, seed=4)
        _, b = run_virtual(120.0, count=200, seed=4)
        self.assertEqual(a.log, b.log)


if __name__ == "__main__":
    unittest.main()
