"""Physical model of the simulated HVAC plant.

Each piece of equipment (an air handling unit, later a chiller, a boiler, ...) is a Module: a small
class that, once per step, turns its internal state and the active faults into the values of its
points. A module only produces points that exist in the catalog (the .db file), so AHU1 (many points)
and AHU2 (a few) use the same model and a new .db entry needs no code change if the module supports its
point name.

The values are physically plausible, not recordings: first-order lags, a PI-controlled cooling coil, fan
affinity law for the static pressure, a slowly loading filter, a day/night cycle (accelerated: see
World.day_seconds), small measurement noise. Faults are flags in `faults[equipment]` that scenarios set.

Adding equipment: write a Module subclass with POINTS and step(), and register it in MODULE_TYPES under
the letters of the equipment name ("CH" for CH1, CH2, ...).
"""
import math
import random
import re


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def lag(current, target, dt, tau):
    """First-order response toward target with time constant tau (s)."""
    if tau <= 0:
        return target
    return current + (target - current) * (1.0 - math.exp(-dt / tau))


class World:
    """Shared conditions: time, day/night cycle, outdoor air, random noise, active faults."""

    def __init__(self, seed=1, day_seconds=1200.0, start_hour=10.0):
        self.t = 0.0                   # simulated seconds since start
        self.day_seconds = day_seconds  # real simulated seconds that make one 24 h day
        self.start_hour = start_hour
        self.rng = random.Random(seed)
        self.faults = {}               # equipment name -> {flag: value}; "*" applies to all equipment
        self.heartbeat = 0

    @property
    def hour(self):
        return (self.start_hour + 24.0 * self.t / self.day_seconds) % 24.0

    def outdoor_temp(self):
        """Mean 31 degC, +-5 over the day, warmest at 15:00 (matches the 31.0 start value of the .db)."""
        return 31.0 + 5.0 * math.sin(2 * math.pi * (self.hour - 9.0) / 24.0)

    def noise(self, sigma):
        return self.rng.gauss(0.0, sigma)

    def fault(self, equipment, key, default=None):
        for scope in (equipment, "*"):
            f = self.faults.get(scope)
            if f and key in f:
                return f[key]
        return default

    def set_fault(self, scope, **flags):
        self.faults.setdefault(scope, {}).update(flags)

    def clear_fault(self, scope, *keys):
        f = self.faults.get(scope, {})
        if not keys:
            f.clear()
        for k in keys:
            f.pop(k, None)


class Module:
    """Base class: POINTS lists the point names the module can produce."""
    POINTS = ()

    def __init__(self, name, records, world):
        self.name = name          # "AHU1"
        self.records = records    # {point: Record} of this equipment
        self.world = world

    def init_from(self, point, default):
        r = self.records.get(point)
        return r.initial if r is not None else default

    def step(self, dt):
        """Advance the model by dt seconds and return {point: value} for points to publish."""
        raise NotImplementedError


class AhuModel(Module):
    """Air handling unit: mixed air -> cooling coil -> heating coil -> supply fan, with a return fan."""
    POINTS = ("SupplyAirTemp", "ReturnAirTemp", "MixedAirTemp", "SupplyAirHumidity", "StaticPressure",
              "FilterDifferentialPressure", "CoolingValveFeedback", "HeatingValveFeedback", "FanSpeedFeedback",
              "OutdoorAirTemp", "SupplyFanRunning", "ReturnFanRunning", "FilterDirty", "SmokeDetected",
              "FreezeStatTrip", "CondensateHighLevel", "CoolingValveOpenProof", "HeatingValveOpenProof",
              "OperatingMode", "DamperPositionStatus")

    def __init__(self, name, records, world):
        super().__init__(name, records, world)
        self.supply = self.init_from("SupplyAirTemp", 14.5)
        self.ret = self.init_from("ReturnAirTemp", 24.0)
        self.mixed = self.init_from("MixedAirTemp", 18.0)
        self.rh = self.init_from("SupplyAirHumidity", 45.0)
        self.speed = self.init_from("FanSpeedFeedback", 65.0)
        self.valve_c = self.init_from("CoolingValveFeedback", 35.0)
        self.valve_h = self.init_from("HeatingValveFeedback", 0.0)
        self.filter_dp = self.init_from("FilterDifferentialPressure", 80.0)
        self.cmd_c = self.valve_c
        self.cmd_h = self.valve_h
        self.integ_c = 0.0
        self.integ_h = 0.0
        self.dirty = self.filter_dp > 250
        self.fan_on = True
        self.ret_fan_on = True
        self.cond = False
        self.phase = world.rng.uniform(0, 2 * math.pi)  # units are not in lockstep

    def step(self, dt):
        w, f = self.world, lambda k, d=None: self.world.fault(self.name, k, d)
        hour = w.hour
        occupied = 6.0 <= hour < 20.0
        warmup = 5.0 <= hour < 6.0

        # --- trips and mode --------------------------------------------------------------
        smoke, freeze = bool(f("smoke")), bool(f("freeze"))
        supply_fail, return_fail = bool(f("supply_fan_fail")), bool(f("return_fan_fail"))
        tripped = smoke or freeze
        self.fan_on = not (tripped or supply_fail)
        self.ret_fan_on = not (tripped or return_fail)
        mode = 0 if tripped else (3 if warmup else (1 if occupied else 2))  # OFF only after a safety trip

        # --- ambient and load ---------------------------------------------------------------
        t_oa = f("oa_temp", w.outdoor_temp())
        load = (1.2 if occupied else 0.2) + f("heat_load", 0.0)
        ret_target = 23.5 + 0.08 * (t_oa - 31.0) + load
        self.ret = lag(self.ret, ret_target, dt, 120.0)
        damper_fault = bool(f("damper_fault")) or freeze
        oa_frac = 1.0 if freeze else (0.25 if mode in (1, 3) else 0.1)
        mixed_target = oa_frac * t_oa + (1 - oa_frac) * self.ret
        self.mixed = lag(self.mixed, mixed_target, dt, 20.0)

        # --- fan ---------------------------------------------------------------------------
        target_speed = 0.0
        if self.fan_on:
            target_speed = (65.0 + 8.0 * math.sin(w.t / 90.0 + self.phase)) if mode in (1, 3) else 45.0
        self.speed = lag(self.speed, target_speed, dt, 8.0 if target_speed else 4.0)
        if self.speed < 0.5:
            self.speed = 0.0

        # --- filter ------------------------------------------------------------------------
        clog = f("filter_clog_rate", 0.0)  # Pa per second, from a scenario
        self.filter_dp += dt * (0.008 + clog) * (self.speed / 65.0) ** 2
        self.filter_dp = clamp(self.filter_dp, 0.0, 1000.0)
        if self.filter_dp > 260.0:
            self.dirty = True
        elif self.filter_dp < 230.0:
            self.dirty = False

        # --- coil control (PI on supply air temperature) --------------------------------------
        sp = 14.5 if mode != 2 else 16.0
        err = self.supply - sp
        cool_capacity = 0.0 if f("cooling_fail") else 1.0
        if self.fan_on and mode:
            self.integ_c = clamp(self.integ_c + 0.4 * err * dt, -20.0, 100.0)
            self.cmd_c = clamp(10.0 * err + self.integ_c, 0.0, 100.0) if err > -1.0 else 0.0
            self.integ_h = clamp(self.integ_h - 0.4 * (err + 1.5) * dt, 0.0, 100.0) if err < -1.5 else max(0.0, self.integ_h - 2 * dt)
            self.cmd_h = clamp(self.integ_h, 0.0, 100.0)
            if err > -1.0:
                self.cmd_h = 0.0
                self.integ_h = 0.0
        else:
            self.cmd_c, self.cmd_h = 0.0, 0.0
        if freeze:  # freeze protection: heating valve open, cooling closed
            self.cmd_c, self.cmd_h = 0.0, 100.0
        stuck = f("cooling_valve_stuck")
        if stuck is not None:
            self.cmd_c = float(stuck)
        self.valve_c = lag(self.valve_c, self.cmd_c, dt, 15.0)
        self.valve_h = lag(self.valve_h, self.cmd_h, dt, 15.0)

        # --- temperatures and humidity -------------------------------------------------------
        coil_c = 30.0 * self.valve_c / 100.0 * cool_capacity
        coil_h = 30.0 * self.valve_h / 100.0
        supply_target = self.mixed - coil_c + coil_h if self.fan_on else lag(self.supply, self.mixed, dt, 60.0)
        self.supply = lag(self.supply, supply_target, dt, 30.0 if self.fan_on else 1e9)
        rh_target = 45.0 + 0.2 * (t_oa - 31.0) - 10.0 * (self.valve_c - 37.0) / 100.0 + f("humidity_add", 0.0)
        self.rh = lag(self.rh, rh_target, dt, 60.0)

        # --- static pressure (fan affinity: ~ speed^2, lower with a loaded filter) --------------
        static = 350.0 * (self.speed / 65.0) ** 2 * (1.0 - 0.0004 * max(0.0, self.filter_dp - 80.0))
        self.cond = bool(f("condensate"))

        noise = w.noise
        vals = {
            "SupplyAirTemp": self.supply + noise(0.05),
            "ReturnAirTemp": self.ret + noise(0.05),
            "MixedAirTemp": self.mixed + noise(0.05),
            "SupplyAirHumidity": self.rh + noise(0.15),
            "StaticPressure": static + noise(2.0) if self.speed else max(0.0, noise(1.0)),
            "FilterDifferentialPressure": self.filter_dp + noise(0.5),
            "CoolingValveFeedback": self.valve_c + noise(0.15),
            "HeatingValveFeedback": self.valve_h + noise(0.1),
            "FanSpeedFeedback": self.speed + (noise(0.2) if self.speed else 0.0),
            "OutdoorAirTemp": t_oa + noise(0.08),
            "SupplyFanRunning": 1 if self.fan_on and self.speed > 1.0 else 0,
            "ReturnFanRunning": 1 if self.ret_fan_on and self.speed > 1.0 else 0,
            "FilterDirty": 1 if self.dirty else 0,
            "SmokeDetected": 1 if smoke else 0,
            "FreezeStatTrip": 1 if freeze else 0,
            "CondensateHighLevel": 1 if self.cond else 0,
            "CoolingValveOpenProof": 1 if self.valve_c > 3.0 else 0,
            "HeatingValveOpenProof": 1 if self.valve_h > 3.0 else 0,
            "OperatingMode": mode,
            "DamperPositionStatus": 3 if damper_fault else (0 if not mode else (2 if oa_frac >= 0.95 else 1)),
        }
        # A dead sensor stops updating (its PV keeps the last value; the application's stale check sees it).
        dead = f("dead_sensors", ())
        return {p: v for p, v in vals.items() if p in self.records and p not in dead}


class SimSupport(Module):
    """Heartbeat and scenario echo for the optional support database (HVAC:SIM:*)."""
    POINTS = ("Heartbeat", "Scenario")

    def __init__(self, name, records, world):
        super().__init__(name, records, world)
        self.scenario_name = "normal"

    def step(self, dt):
        self.world.heartbeat += 1
        return {p: v for p, v in {"Heartbeat": self.world.heartbeat, "Scenario": self.scenario_name}.items() if p in self.records}


# Equipment name = letters + number. The letters select the model.
MODULE_TYPES = {"AHU": AhuModel, "SIM": SimSupport}
_EQUIP = re.compile(r"^([A-Za-z]+)(\d*)$")


def build_modules(catalog, world):
    """One module per piece of equipment found in the catalog; equipment without a model is reported."""
    modules, unmodelled = [], []
    for (prefix, equip), records in sorted(catalog.by_equipment().items()):
        m = _EQUIP.match(equip)
        cls = MODULE_TYPES.get(m.group(1).upper()) if m else None
        if cls is None:
            unmodelled.append(equip)
            continue
        modules.append(cls(equip, records, world))
    return modules, unmodelled
