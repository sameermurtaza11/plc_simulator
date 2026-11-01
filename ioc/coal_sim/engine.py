"""The simulator: unit load, equipment state, faults, and a per-point random feed schedule.

Feed timing. Every point has its own update delay, drawn at random between RATE_MIN (0.25 s) and RATE_MAX (5 s)
and drawn again for every single update. Two things make the timing change over time:
  * each point has a "personality" (its typical delay) that is drawn again every 45 to 240 s, so a point that
    was fast becomes slow later and the other way round;
  * a slow plant-wide activity wave speeds all points up and down by up to +-30 %.
A delay never leaves the 0.25 to 5 s range (the wave is applied first, then the delay is clamped).

Values. A point follows the unit load (itself a slow random programme of ramps and plateaus) through a first-order
lag with measurement noise and a slow drift. A switched machine (mill, fan, pump ...) runs when the load is above
its threshold and trips now and then; a stopped machine falls to its "off" value. Now and then a random point is
driven to one of its alarm limits for 30 to 150 s, so the IOC raises real native alarms.

The engine only decides values. How they reach the IOC is the job of the writer (hvac_sim.writers); the
monitoring application never writes to the IOC.
"""
import heapq
import json
import math
import os
import random
import time

RATE_MIN = 0.25
RATE_MAX = 5.0
REFRESH_S = 10.0     # a binary point whose state did not change is sent again this often (refills a restarted IOC)
SEV = {"NO_ALARM": 0, "MINOR": 1, "MAJOR": 2}


class PointState:
    __slots__ = ("p", "value", "drift", "last", "rate", "redraw", "ov_target", "ov_until", "sw_until", "state", "sent", "sent_t")

    def __init__(self, p):
        self.p = p
        self.value = p.initial
        self.drift = 0.0
        self.last = 0.0
        self.rate = 1.0
        self.redraw = 0.0
        self.ov_target = None
        self.ov_until = 0.0
        self.sw_until = 0.0
        self.state = p.value0
        self.sent = None
        self.sent_t = -1e9


class EquipState:
    __slots__ = ("e", "running", "tripped", "trip_until")

    def __init__(self, e):
        self.e = e
        self.running = True
        self.tripped = False
        self.trip_until = 0.0


class Engine:
    def __init__(self, plant, seed=1, fault_rate_h=30.0, start_load=0.85, log=print):
        self.plant = plant
        self.rng = random.Random(seed)
        self.log = log
        self.fault_rate_h = fault_rate_h
        self.t0 = None
        self.load = start_load
        self.load_target = start_load
        self.load_rate = 0.0          # per second
        self.next_programme = 0.0
        self.pinned = None            # load fixed by the control file
        self.wave_phase = self.rng.uniform(0, 6.283)
        self.eq = {k: EquipState(e) for k, e in plant.equips.items()}
        self.ps = [PointState(p) for p in plant.points]
        self.by_equip = {}
        for s in self.ps:
            self.by_equip.setdefault(s.p.equip, []).append(s)
        self._tick_t = None
        self.events = []              # (time, text) of the faults, for the log
        for s in self.ps:
            self._new_personality(s, 0.0)
            s.last = 0.0
        self._update_equipment(0.0, 0.0)
        for s in self.ps:             # start on the plateau, not on a cold start
            s.value = self._base(s, self.load)

    # -- timing -----------------------------------------------------------------------------------
    def _new_personality(self, s, now):
        s.rate = self.rng.uniform(RATE_MIN, RATE_MAX)
        s.redraw = now + self.rng.uniform(45.0, 240.0)

    def wave(self, now):
        return 1.0 + 0.3 * math.sin(now / 60.0 * 2 * math.pi / 3.0 + self.wave_phase)

    def next_delay(self, s, now):
        """The delay until this point is fed again: random, within 0.25 to 5 s, different every time."""
        if now >= s.redraw:
            self._new_personality(s, now)
        d = s.rate * self.rng.uniform(0.6, 1.4) / self.wave(now)
        return min(RATE_MAX, max(RATE_MIN, d))

    # -- unit load, equipment, faults (called about twice per second) ---------------------------------
    def tick(self, now):
        if self._tick_t is not None and now - self._tick_t < 0.5:
            return
        dt = 0.5 if self._tick_t is None else now - self._tick_t
        self._tick_t = now
        if self.pinned is not None:
            self.load = self.pinned
        else:
            if now >= self.next_programme:
                self.load_target = self.rng.choice([self.rng.uniform(0.5, 0.7), self.rng.uniform(0.7, 0.9), self.rng.uniform(0.85, 1.0)])
                ramp = self.rng.uniform(40.0, 150.0)
                self.load_rate = (self.load_target - self.load) / ramp
                self.next_programme = now + ramp + self.rng.uniform(60.0, 360.0)
                self.log("t=%6.0fs unit load programme: %.0f %% -> %.0f %%" % (now, self.load * 100, self.load_target * 100))
            if (self.load_rate > 0 and self.load < self.load_target) or (self.load_rate < 0 and self.load > self.load_target):
                self.load += self.load_rate * dt
        self._update_equipment(now, dt)
        if self.fault_rate_h > 0 and self.rng.random() < self.fault_rate_h / 3600.0 * dt:
            self._start_fault(now)

    def _update_equipment(self, now, dt):
        for k, es in self.eq.items():
            e = es.e
            if e.always:
                es.running = True
                continue
            if es.tripped:
                if now >= es.trip_until:
                    es.tripped = False
                    self.log("t=%6.0fs %s restarted after trip" % (now, e.label))
            elif e.trip_h > 0 and es.running and self.rng.random() < e.trip_h / 3600.0 * dt:
                es.tripped = True
                es.trip_until = now + self.rng.uniform(60.0, 180.0)
                self.log("t=%6.0fs %s TRIPPED" % (now, e.label))
            want = self.load >= e.threshold + (0.0 if not es.running else -0.03)
            es.running = want and not es.tripped

    def _start_fault(self, now):
        for _ in range(20):
            s = self.rng.choice(self.ps)
            p = s.p
            if p.rtype != "ai" or not p.lim or s.ov_until > now:
                continue
            if not self.eq[p.equip].running:
                continue
            if "LOW" in p.lim and self.rng.random() < 0.3:
                key = "LOLO" if self.rng.random() < 0.3 else "LOW"
                tgt = p.lim[key] - 0.15 * (p.lim[key] - p.lopr)
            else:
                key = "HIHI" if self.rng.random() < 0.3 else "HIGH"
                tgt = p.lim[key] + 0.15 * (p.hopr - p.lim[key])
            s.ov_target = tgt
            s.ov_until = now + self.rng.uniform(30.0, 150.0)
            self.events.append((now, "%s -> %s" % (p.name, key)))
            self.log("t=%6.0fs fault: %s driven to its %s limit (%.1f %s) for %.0f s" % (now, p.name, key, tgt, p.egu, s.ov_until - now))
            return

    # -- values -----------------------------------------------------------------------------------
    def _base(self, s, load):
        p = s.p
        if p.off is not None and not self.eq[p.equip].running:
            return p.off
        return p.idle + (p.nom - p.idle) * (max(0.0, load) ** p.shape)

    def compute(self, s, now):
        """The value to send for this point now (a number for ai, 0 or 1 for bi)."""
        p = s.p
        dt = max(1e-3, now - s.last)
        s.last = now
        es = self.eq[p.equip]
        if p.rtype == "bi":
            return self._digital(s, es, now, dt)
        g = self.rng.gauss
        s.drift = s.drift * math.exp(-dt / 300.0) + g(0.0, p.sigma * 0.3) * math.sqrt(min(dt, 5.0))
        if now < s.ov_until:
            target = s.ov_target
        else:
            target = self._base(s, self.load) + s.drift
        s.value += (target - s.value) * (1.0 - math.exp(-dt / max(0.5, p.tau)))
        v = s.value + g(0.0, p.sigma)
        v = max(p.lopr, min(p.hopr, v))
        return round(v, p.prec) if p.prec else int(round(v))

    def _digital(self, s, es, now, dt):
        p = s.p
        k = p.kind
        if k == "run" or k == "follow":
            return 1 if es.running else 0
        if k == "trip":
            return 1 if es.tripped else 0
        if k == "const":
            return p.value0
        if k == "sw":
            if s.state == 1 and now >= s.sw_until:
                s.state = 0
            elif s.state == 0 and es.running and p.rate_h > 0 and self.rng.random() < p.rate_h / 3600.0 * dt:
                s.state = 1
                s.sw_until = now + self.rng.uniform(0.5, 1.5) * p.dur
            return s.state
        return 0

    # -- expected alarm (the model of the record configuration; the real alarm comes from the IOC) ------
    def expected_alarms(self):
        out = {}
        for s in self.ps:
            p = s.p
            v = s.sent
            if v is None:
                continue
            if p.rtype == "bi":
                sev = SEV.get(p.osv if v else p.zsv, 0)
            else:
                sev = 0
                for key, ks in (("HIHI", 2), ("HIGH", 1), ("LOLO", 2), ("LOW", 1)):
                    lim = p.lim.get(key)
                    if lim is not None and ((key in ("HIHI", "HIGH") and v >= lim) or (key in ("LOLO", "LOW") and v <= lim)):
                        sev = max(sev, ks)
            if sev:
                out[p.name] = sev
        return out


class Simulator:
    def __init__(self, plant, writer, seed=1, fault_rate_h=30.0, control_file=None, log=print, status_every=10.0):
        self.engine = Engine(plant, seed, fault_rate_h, log=log)
        self.writer, self.log = writer, log
        self.control_file = control_file
        self._control_mtime = 0.0
        self.status_every = status_every
        self.heap = []
        self.writes = 0
        self.delays = []          # delays scheduled since the last status line
        self.failed = 0

    def _poll_control(self):
        cf = self.control_file
        if not cf or not os.path.exists(cf):
            return
        m = os.path.getmtime(cf)
        if m == self._control_mtime:
            return
        self._control_mtime = m
        try:
            with open(cf, encoding="utf-8") as fh:
                load = json.load(fh).get("load")
        except (OSError, ValueError):
            return
        self.engine.pinned = None if load is None else max(0.0, min(1.0, float(load)))
        self.log("unit load %s" % ("released to the programme" if load is None else "pinned at %.0f %%" % (self.engine.pinned * 100)))

    def _feed(self, s, now):
        """Compute and send one point; returns False if the writer could not deliver."""
        v = self.engine.compute(s, now)
        if s.p.rtype == "bi" and v == s.sent and now - s.sent_t < REFRESH_S:
            return True
        if self.writer.put(s.p.name, v) is False:
            self.failed += 1
            return False
        s.sent, s.sent_t = v, now
        self.writes += 1
        return True

    def run(self, duration=None, clock=time.monotonic, sleep=time.sleep):
        eng = self.engine
        start = clock()
        t = 0.0
        n = len(eng.ps)
        self.log("coal plant simulator: %d PVs, feed delay %.2f to %.1f s per point, fault rate %.0f/h, writer %s"
                 % (n, RATE_MIN, RATE_MAX, eng.fault_rate_h, self.writer.name))
        for i, s in enumerate(eng.ps):    # first full push, spread over the first seconds
            heapq.heappush(self.heap, (eng.rng.uniform(0.0, 3.0), i))
        last_status = 0.0
        w0 = 0
        try:
            while duration is None or t < duration:
                t = clock() - start
                self._poll_control()
                eng.tick(t)
                while self.heap and self.heap[0][0] <= t:
                    _, i = heapq.heappop(self.heap)
                    s = eng.ps[i]
                    self._feed(s, t)
                    d = eng.next_delay(s, t)
                    self.delays.append(d)
                    heapq.heappush(self.heap, (t + d, i))
                if t - last_status >= self.status_every:
                    last_status = t
                    d = self.delays or [0.0]
                    alarms = eng.expected_alarms()
                    self.log("t=%6.0fs load %3.0f %%  %5.0f writes/s  delay min/mean/max %.2f/%.2f/%.2f s  running %d/%d machines  "
                             "expected alarms %d (major %d)  send failures %d"
                             % (t, eng.load * 100, (self.writes - w0) / max(1e-9, self.status_every), min(d), sum(d) / len(d), max(d),
                                sum(1 for e in eng.eq.values() if e.running and not e.e.always),
                                sum(1 for e in eng.eq.values() if not e.e.always),
                                len(alarms), sum(1 for v in alarms.values() if v == 2), self.failed))
                    self.delays, w0 = [], self.writes
                nxt = self.heap[0][0] if self.heap else t + 0.05
                sleep(max(0.0, min(0.05, nxt - (clock() - start))))
        except KeyboardInterrupt:
            self.log("stopped")
        finally:
            self.writer.close()
