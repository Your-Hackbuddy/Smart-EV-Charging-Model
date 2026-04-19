"""
╔══════════════════════════════════════════════════════╗
║      SMART EV POWER CHARGING STATION                 ║
║     Background Decision Engine + Priority Queue      ║
╚══════════════════════════════════════════════════════╝
"""

# ════════════════════════════════════════════════════════════════════════════
# BLOCK 1 │ STANDARD LIBRARY IMPORTS
# ────────────────────────────────────────────────────────────────────────────
# datetime   – timestamps for sessions, booking schedules, and slot windows
# threading  – runs the emergency-stop keyboard listener in the background
#              so the main charging loop can keep animating without blocking
# time       – controls animation speed (sleep) and cool-down loop timing
# random     – simulates battery levels, ambient temperature, grid faults
# json       – reads / writes charging history and booking records to disk
# os         – checks whether JSON files exist before trying to open them
# sys        – writes live progress bar frames directly to stdout in-place
# dataclass  – reduces boilerplate for the Vehicle and EVState value objects
# typing     – type hints (Optional, List, Dict) for readability and safety
# Enum       – gives ChargeRate named symbolic values instead of raw strings
# ════════════════════════════════════════════════════════════════════════════

from datetime import datetime, timedelta
import threading
import time
import random
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 2 │ GLOBAL CONSTANTS
# ────────────────────────────────────────────────────────────────────────────
# BATTERY_CAPACITY_KWH – physical size of the simulated EV battery (kWh).
#                        Converts charged-% → kWh → ₹ cost in the receipt.
# HISTORY_FILE         – JSON-lines file; each session appends one line.
# BOOKING_FILE         – JSON array that stores all slot bookings + statuses.
# GRACE_PERIOD_MIN     – minutes a booked user has to check in before the
#                        system auto-cancels their reservation.
# GRID_MAX_KW          – total kW the station grid can supply simultaneously.
# AC_CHARGER_KW        – maximum output of one AC charger port  (7.2 kW).
# DC_CHARGER_KW        – maximum output of one DC fast-charger  (50.0 kW).
# MAX_VEHICLES         – how many vehicles the station can serve at once.
# TEMP_AMBIENT         – randomised outdoor temperature for this run (28–42°C).
# TEMP_WARN            – battery temp that triggers a soft warning (50°C).
# TEMP_CRITICAL        – battery temp that forces a charging pause  (52°C).
# TEMP_RESUME          – battery must cool to this before charging resumes (45°C).
# ════════════════════════════════════════════════════════════════════════════

BATTERY_CAPACITY_KWH = 30.0
HISTORY_FILE         = "charging_history.json"
BOOKING_FILE         = "slot_bookings.json"
GRACE_PERIOD_MIN     = 5
GRID_MAX_KW          = 150.0
AC_CHARGER_KW        = 7.2
DC_CHARGER_KW        = 50.0
MAX_VEHICLES         = 3
TEMP_AMBIENT         = random.randint(28, 42)
TEMP_WARN            = 50
TEMP_CRITICAL        = 52
TEMP_RESUME          = 45


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 3 │ TIERED PRICING TABLES  (₹ per kWh)
# ────────────────────────────────────────────────────────────────────────────
# Pricing logic — rate follows CHARGING SPEED delivered to the user:
#
#   FULL      → 100% speed, no grid constraint    → base / standard rate
#   THROTTLED → 75%  speed, grid slightly stressed → higher than FULL
#                  charger works harder under pressure to sustain near-full
#                  throughput; user pays a premium for priority delivery
#   SLOW      → 50%  speed, grid heavily loaded   → lower than THROTTLED
#                  user receives noticeably reduced power; discounted rate
#   QUEUED    → 0%   speed, waiting for a slot    → lowest rate of all
#                  compensation discount for waiting time
#
#   Pre-book  → +₹2/kWh surcharge on top of whichever rate applies,
#               charged for the guarantee of having a reserved slot.
#
# AC_RATE  – rates for the AC slow-charger port  (7.2 kW)
# DC_RATE  – rates for the DC fast-charger port (50.0 kW)
#            DC is more expensive per kWh — delivers far more power per
#            minute, which costs more to sustain at the hardware level.
# ════════════════════════════════════════════════════════════════════════════

AC_RATE = {
    "FULL"     : 18.0,   # base rate       — full speed, no congestion
    "THROTTLED": 16.0,   # -₹2.0 vs FULL  — grid stressed, reduced delivery
    "SLOW"     : 15.0,   # -₹3.0 vs FULL  — heavily loaded, lower charge
    "QUEUED"   : 13.0,   # lowest rate     — waiting compensation discount
}
DC_RATE = {
    "FULL"     : 22.0,   # base rate       — full speed, no congestion
    "THROTTLED": 20.0,   # -₹2.0 vs FULL  — grid stressed, reduced delivery
    "SLOW"     : 19.0,   # -₹3.0 vs FULL  — heavily loaded, lower charge
    "QUEUED"   : 17.5,   # lowest rate     — waiting compensation discount
}
BOOKING_SURCHARGE_PER_KWH = 2.0   # +₹2/kWh on any rate for pre-booked slots


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 4 │ THROTTLE SPEED MAP
# ────────────────────────────────────────────────────────────────────────────
# Maps each ChargeRate name → a speed multiplier (0.0 – 1.0).
# Used in dual_bar() to control how fast the progress bar advances:
#   adjusted_delay = base_delay / throttle
# Lower throttle → longer delay → slower animation → mirrors real power delivery.
#
#   FULL      = 1.00 → 100% of charger output delivered
#   THROTTLED = 0.75 → 75%  of charger output (grid slightly over capacity)
#   SLOW      = 0.50 → 50%  of charger output (grid heavily loaded)
#   QUEUED    = 0.30 → placeholder; vehicle is waiting, no real power yet
# ════════════════════════════════════════════════════════════════════════════

_THROTTLE_MAP = {
    "FULL"     : 1.00,
    "THROTTLED": 0.75,
    "SLOW"     : 0.50,
    "QUEUED"   : 0.30,
}


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 5 │ CHARGE RATE ENUM
# ────────────────────────────────────────────────────────────────────────────
# Symbolic names for the four possible charging speeds assigned by the grid.
# Using an Enum instead of raw strings catches typos at import time and
# makes comparisons type-safe throughout the codebase.
#   FULL      – charger running at 100% capacity
#   THROTTLED – charger running at 75%  (grid slightly over capacity)
#   SLOW      – charger running at 50%  (grid heavily loaded)
#   QUEUED    – vehicle waiting; no power allocated yet
# ════════════════════════════════════════════════════════════════════════════

class ChargeRate(Enum):
    FULL      = "Full rate"
    THROTTLED = "Throttled (75%)"
    SLOW      = "Slow (50%)"
    QUEUED    = "Queued — waiting for slot"


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 6 │ VEHICLE DATACLASS
# ────────────────────────────────────────────────────────────────────────────
# Represents a single EV at the station with its key attributes.
# _score is computed once in __post_init__ and cached so the allocation
# engine can sort N vehicles without recomputing the formula N² times.
#
# _calc_priority() scoring rules:
#   base   = (100 - battery) × 2   → emptier battery = higher urgency
#   +10 pts if health < 80%         → degraded battery needs earlier care
#   +30 pts if battery ≤ 20%        → critically low — jump to front of queue
#   +10 pts if battery ≤ 50%        → moderately low — small boost
#
# label() – colour-coded one-line summary for the station status dashboard.
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Vehicle:
    vid       : str
    battery   : int
    health    : int
    voltage   : int
    arrival   : datetime = field(default_factory=datetime.now)
    is_booked : bool     = field(default=False)
    _score    : float    = field(init=False, repr=False)

    def __post_init__(self):
        self._score = self._calc_priority()

    def _calc_priority(self) -> float:
        score = (100 - self.battery) * 2.0
        if self.health < 80:
            score += 10
        if self.battery <= 20:
            score += 30
        elif self.battery <= 50:
            score += 10
        return round(score, 1)

    @property
    def priority_score(self) -> float:
        return self._score

    def label(self) -> str:
        b           = "🔴" if self.battery <= 20 else ("🟡" if self.battery <= 50 else "🟢")
        booked_tag  = "  📋 BOOKED" if self.is_booked else ""
        return f"  {self.vid:<6} Bat:{self.battery:>3}% {b}  Health:{self.health}%  Score:{self.priority_score}{booked_tag}"


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 7 │ EV STATE — current user's vehicle + display helpers
# ────────────────────────────────────────────────────────────────────────────
# Simulates the arriving user's vehicle by randomising battery (5–95%),
# health (60–95%), and voltage (150–500V) at startup.
#   show_welcome()     – prints the station banner on first launch.
#   show_car_details() – pretty-prints battery bar, health, voltage, and
#                        ambient temperature with colour-coded status icons.
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class EVState:
    battery : int = field(default_factory=lambda: random.randint(5, 95))
    health  : int = field(default_factory=lambda: random.randint(60, 95))
    voltage : int = field(default_factory=lambda: random.randint(150, 500))

    def show_welcome(self):
        print(f"\n{'═'*48}")
        print(f"    WELCOME TO SMART CHARGING STATION ")
        print(f"{'═'*48}\n")

    def show_car_details(self):
        bar = "█" * (self.battery // 10) + "░" * (10 - self.battery // 10)
        h   = "🟢" if self.health  >= 80 else ("🟡" if self.health  >= 70 else "🔴")
        v   = "🟢" if self.voltage >  200 else "🔴"
        t   = "🟡" if TEMP_AMBIENT >= 38  else "🟢"
        print(f"\n{'─'*48}")
        print(f"   Vehicle Details")
        print(f"{'─'*48}")
        print(f"  Battery      [{bar}]  {self.battery}%")
        print(f"  Health       {h}  {self.health}%")
        print(f"  Voltage      {v}  {self.voltage}V")
        print(f"  Ambient      {t}  {TEMP_AMBIENT}°C")
        print(f"  Time         {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'─'*48}\n")


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 8 │ TEMPERATURE SIMULATOR
# ────────────────────────────────────────────────────────────────────────────
# Models how a battery's internal temperature rises and falls during charging.
# DC sessions heat up faster (peak_offset 12–18°C above ambient) than AC
# sessions (5–9°C), matching real-world fast-charge heat profiles.
#
#   step(progress)  – called each tick; returns current temperature using a
#                     bell-curve shape: rises to 70% charge, then gradually
#                     falls as the BMS reduces current near full.
#   cool_down()     – called during overheat pause; drops temp by 0.5–1.5°C
#                     per tick until it reaches TEMP_RESUME (45°C).
#   status(t)       – returns 🟢 NORMAL / 🟡 WARM / 🔴 CRITICAL label.
#   bar(t, w)       – ASCII heat bar scaled between ambient and TEMP_CRITICAL.
# ════════════════════════════════════════════════════════════════════════════

class TempSimulator:
    def __init__(self, ambient: float, charge_type: str):
        self.temp        = ambient + random.uniform(2, 5)
        self.ambient     = ambient
        self.is_dc       = "DC" in charge_type
        self.peak_offset = (
            random.uniform(12, 18) if self.is_dc else random.uniform(5, 9)
        )

    def step(self, progress: float) -> float:
        rise = self.peak_offset * (
            progress / 0.7 if progress < 0.7
            else 1 - (progress - 0.7) / 0.3 * 0.4
        )
        self.temp = self.ambient + rise + random.uniform(-0.3, 0.3)
        return round(self.temp, 1)

    def cool_down(self) -> float:
        self.temp = max(self.ambient + 2, self.temp - random.uniform(0.5, 1.5))
        return round(self.temp, 1)

    def status(self, t: float) -> str:
        if t >= TEMP_CRITICAL: return "🔴 CRITICAL"
        if t >= TEMP_WARN:     return "🟡 WARM"
        return "🟢 NORMAL"

    def bar(self, t: float, w: int = 22) -> str:
        ratio  = max(0.0, min(1.0, (t - self.ambient) / (TEMP_CRITICAL - self.ambient + 5)))
        filled = int(ratio * w)
        ch     = "█" if t >= TEMP_CRITICAL else ("▓" if t >= TEMP_WARN else "░")
        return ch * filled + "·" * (w - filled)


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 9 │ GRID DECISION ENGINE
# ────────────────────────────────────────────────────────────────────────────
# Central brain of the station — owns all vehicles and decides who gets
# how much power at what rate.
#
#   __init__               – creates the engine and populates simulated load.
#   _simulate_station_load – adds 1–(MAX_VEHICLES-1) random vehicles already
#                            charging when the user arrives.
#   add_vehicle            – registers the user's EV with a unique "EV-XXX" ID
#                            (3-digit suffix avoids collision with simulated "EV-XX" 2-digit IDs);
#                            if station is full, immediately assigns QUEUED;
#                            a fresh allocation pass.
#   _available_grid_kw     – returns usable kW scaled by input voltage:
#                            ≤200V→40%  ≤350V→75%  >350V→100% of GRID_MAX_KW
#   _run_allocation        – sorts all vehicles by priority score (highest first),
#                            greedily assigns power until grid is full:
#                              DC eligible: voltage>200 AND health≥80 AND battery≤50
#                              FULL      → rem ≥ charger_kw
#                              THROTTLED → rem ≥ charger_kw × 0.5
#                              SLOW      → 0 < rem < charger_kw × 0.5
#                              QUEUED    → rem = 0
#   _estimate_wait         – rough wait (minutes) from remaining charge of
#                            higher-priority vehicles ahead in the queue.
#   suggest_free_slots     – projects future grid load across 30-min windows
#                            so the pre-booking UI can show cheapest times.
#   show_station_status    – prints full station dashboard: grid load bar,
#                            pricing table, and ranked vehicle queue.
#   my_allocation          – returns the allocation dict for a given vehicle ID.
# ════════════════════════════════════════════════════════════════════════════

class GridDecisionEngine:
    def __init__(self, grid_voltage: int, reserved_slot: bool = False):
        self.grid_voltage  = grid_voltage
        self.reserved_slot = reserved_slot   # True → booked user arriving; cap simulate at 1
        self.vehicles      : List[Vehicle] = []
        self.allocations   : Dict[str, dict] = {}
        self._grid_used_kw : float = 0.0
        self._simulate_station_load()

    def _simulate_station_load(self):
        # Always cap at MAX_VEHICLES - 1 so the arriving user always has a slot.
        # reserved_slot=True (booked) → same cap, grid rebuilt before add_vehicle anyway.
        # Use getattr for safety — __new__() bypasses __init__ in pre_book_slot temp engine.
        n = random.randint(1, MAX_VEHICLES - 1)   # 1 or 2 → always leaves room for user
        for _ in range(n):
            v = Vehicle(
                vid     = f"EV-{random.randint(10, 99)}",
                battery = random.randint(10, 90),
                health  = random.randint(65, 95),
                voltage = self.grid_voltage,
                arrival = datetime.now() - timedelta(minutes=random.randint(5, 40)),
            )
            self.vehicles.append(v)

    def add_vehicle(self, state: EVState, is_booked: bool = False, vid_override: str = None) -> str:
        # Booked user keeps their original Car ID as VID.
        # Walk-in gets a fresh random EV-XX that doesn't collide with existing VIDs.
        if vid_override:
            vid = vid_override.upper()
        else:
            vid = f"EV-{random.randint(10, 99)}"
            while vid in {v.vid for v in self.vehicles}:
                vid = f"EV-{random.randint(10, 99)}"
        v   = Vehicle(vid=vid, battery=state.battery, health=state.health, voltage=state.voltage, is_booked=is_booked)
        self.vehicles.append(v)

        if len(self.vehicles) >= MAX_VEHICLES:
            sorted_veh = sorted(self.vehicles, key=lambda x: x.priority_score, reverse=True)
            avail_kw   = self._available_grid_kw()
            self.allocations[vid] = {
                "vehicle"    : v,
                "charge_type": "AC",
                "rate"       : ChargeRate.QUEUED,
                "kw"         : 0.0,
                "wait_min"   : self._estimate_wait(sorted_veh, v, avail_kw),
                "priority"   : v.priority_score,
            }
            return vid

        self._run_allocation()
        return vid

    def _available_grid_kw(self) -> float:
        if self.grid_voltage <= 200:   return GRID_MAX_KW * 0.4
        elif self.grid_voltage <= 350: return GRID_MAX_KW * 0.75
        return GRID_MAX_KW

    def _run_allocation(self):
        available_kw = self._available_grid_kw()
        sorted_veh   = sorted(self.vehicles, key=lambda v: v.priority_score, reverse=True)
        used_kw      = 0.0

        for v in sorted_veh:
            # DC eligible: strong voltage + healthy battery + not too full
            use_dc     = (v.voltage > 200 and v.health >= 80 and v.battery <= 50)
            charger_kw = DC_CHARGER_KW if use_dc else AC_CHARGER_KW
            rem        = available_kw - used_kw

            if rem <= 0:                    rate, actual = ChargeRate.QUEUED,    0.0
            elif rem >= charger_kw:         rate, actual = ChargeRate.FULL,      charger_kw
            elif rem >= charger_kw * 0.5:   rate, actual = ChargeRate.THROTTLED, min(charger_kw * 0.75, rem)
            else:                           rate, actual = ChargeRate.SLOW,      min(charger_kw * 0.5,  rem)

            used_kw += actual
            self.allocations[v.vid] = {
                "vehicle"    : v,
                "charge_type": "DC" if use_dc else "AC",
                "rate"       : rate,
                "kw"         : round(actual, 1),
                "wait_min"   : self._estimate_wait(sorted_veh, v, available_kw) if rate == ChargeRate.QUEUED else 0,
                "priority"   : v.priority_score,
            }

        self._grid_used_kw = used_kw

    def _estimate_wait(self, sorted_veh: list, target: Vehicle, avail_kw: float) -> int:
        # FIX: use >= so vehicles with equal priority score are also counted ahead
        # (strict > caused tied vehicles to show wait_min=0 even when slots were full)
        ahead = [v for v in sorted_veh if v.priority_score >= target.priority_score and v.vid != target.vid]
        if not ahead:
            return 0
        avg = sum((100 - v.battery) for v in ahead[:2]) / max(len(ahead[:2]), 1)
        return max(5, int(avg * 0.8))

    def suggest_free_slots(self, num_slots: int = 6) -> list:
        # Estimate finish time per vehicle:
        #   remaining_kwh = ((100 - battery) / 100) × BATTERY_CAPACITY_KWH
        #   hours         = remaining_kwh / charger_kw
        # Count vehicles still charging per 30-min window → load% → rate tier.
        # Returns windows sorted emptiest-first (cheapest options on top).
        now       = datetime.now()
        slot_data = []

        finish_times = []
        for v in self.vehicles:
            alloc = self.allocations.get(v.vid, {})
            kw    = alloc.get("kw", AC_CHARGER_KW) or AC_CHARGER_KW
            remaining_kwh   = ((100 - v.battery) / 100) * BATTERY_CAPACITY_KWH
            finish_times.append(now + timedelta(hours=remaining_kwh / kw))

        for i in range(num_slots):
            window_start = now + timedelta(minutes=30 * i)
            window_end   = window_start + timedelta(minutes=30)
            busy         = sum(1 for ft in finish_times if ft > window_start)
            load_pct     = min(100, int((busy / MAX_VEHICLES) * 100))

            if load_pct <= 40:
                rate_label, charge_key = "🟢 Low load — Full speed",    "FULL"
            elif load_pct <= 70:
                rate_label, charge_key = "🟡 Moderate — Throttled",      "THROTTLED"
            elif load_pct <= 90:
                rate_label, charge_key = " High load — Slow charge",   "SLOW"
            else:
                rate_label, charge_key = "🔴 Full — You will be Queued", "QUEUED"

            slot_data.append({
                "window_start": window_start,
                "window_end"  : window_end,
                "busy_count"  : busy,
                "load_pct"    : load_pct,
                "rate_label"  : rate_label,
                "charge_key"  : charge_key,
            })

        return sorted(slot_data, key=lambda s: s["load_pct"])

    def show_station_status(self, my_vid: str):
        avail    = self._available_grid_kw()
        used     = self._grid_used_kw
        # FIX: clamp to 100 so bar arithmetic (10 - load_pct//10) never goes negative
        load_pct = min(100, int((used / avail) * 100) if avail > 0 else 100)
        bar      = "█" * (load_pct // 10) + "░" * (10 - load_pct // 10)

        print(f"{'─'*54}")
        print(f"   STATION GRID STATUS")
        print(f"{'─'*54}")
        print(f"  Grid voltage   : {self.grid_voltage}V")
        print(f"  Available power: {avail:.0f} kW")
        print(f"  In use         : {used:.1f} kW  [{bar}] {load_pct}%")
        print(f"  Vehicles       : {len(self.vehicles)} / {MAX_VEHICLES} max")
        print(f"{'─'*54}")
        print(f"   Per Unit Cost  (₹/kWh)  — tiered by grid load")
        print(f"{'─'*54}")
        print(f"  {'Mode':<22} {'AC':>6}    {'DC':>6}   Note")
        print(f"  {'─'*50}")
        print(f"  {'Full rate':<22} ₹{AC_RATE['FULL']:>5.1f}    ₹{DC_RATE['FULL']:>5.1f}   Standard")
        print(f"  {'Throttled (75%)':<22} ₹{AC_RATE['THROTTLED']:>5.1f}    ₹{DC_RATE['THROTTLED']:>5.1f}   Grid stressed")
        print(f"  {'Slow (50%)':<22} ₹{AC_RATE['SLOW']:>5.1f}    ₹{DC_RATE['SLOW']:>5.1f}   Reduced delivery")
        print(f"  {'Queued (waiting)':<22} ₹{AC_RATE['QUEUED']:>5.1f}    ₹{DC_RATE['QUEUED']:>5.1f}   Wait compensation")
        print(f"  {'─'*50}")
        print(f"  * Pre-booking adds ₹{BOOKING_SURCHARGE_PER_KWH:.1f}/kWh surcharge for slot guarantee")
        print(f"{'─'*54}")

        sorted_veh = sorted(self.vehicles, key=lambda v: v.priority_score, reverse=True)
        print(f"  Priority Queue (highest → lowest):\n")
        for rank, v in enumerate(sorted_veh, 1):
            alloc  = self.allocations.get(v.vid, {})
            rate   = alloc.get("rate", ChargeRate.QUEUED)
            kw     = alloc.get("kw", 0)
            marker = " ← YOU" if v.vid == my_vid else ""
            print(f"  #{rank}  {v.label()}  [{rate.value}  {kw}kW]{marker}")
        print(f"{'─'*54}\n")

    def my_allocation(self, vid: str) -> dict:
        return self.allocations.get(vid, {})


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 10 │ POWER MONITOR
# ────────────────────────────────────────────────────────────────────────────
# Simulates real-time voltage and power quality during a charge session.
# A random fault is scheduled at a random tick (15–70%) through the session
# to mimic real-world grid anomalies.
#
# Fault types (integer codes):
#   ISSUE_NONE      (0) – everything normal
#   ISSUE_VOLT_WARN (1) – voltage dipped to 72% of nominal → warning only
#   ISSUE_VOLT_CRIT (2) – voltage dipped to 50% of nominal → auto-stop
#   ISSUE_PWR_WARN  (3) – power fell to 45% of nominal     → warning only
#   ISSUE_PWR_CRIT  (4) – power fell to 25% of nominal     → auto-stop
#   ISSUE_TECH      (5) – random technical fault message    → auto-stop
#
# step(idx)     – adds small noise to voltage/power each tick, injects the
#                 scheduled fault at the right tick, returns
#                 (voltage, power_kw, issue_code, message).
# is_critical() – True for fault codes that halt charging immediately.
# icon()        – 🟢 / 🟡 / 🔴 badge based on severity.
# stop_reason() – maps critical issue codes to string labels for the receipt.
# ════════════════════════════════════════════════════════════════════════════

class PowerMonitor:
    VOLTAGE_WARN    = 180
    VOLTAGE_CRIT    = 140
    POWER_WARN      = 0.55
    POWER_CRIT      = 0.35
    ISSUE_NONE      = 0
    ISSUE_VOLT_WARN = 1
    ISSUE_VOLT_CRIT = 2
    ISSUE_PWR_WARN  = 3
    ISSUE_PWR_CRIT  = 4
    ISSUE_TECH      = 5

    _TECH_FAULTS = [
        "Connector loose — check plug",
        "CAN bus timeout detected",
        "BMS communication error",
        "Grid frequency deviation",
        "Charger thermal overload",
    ]

    def __init__(self, nominal_voltage: int, nominal_kw: float):
        self.nominal_v  = nominal_voltage
        self.nominal_kw = nominal_kw
        self.voltage    = float(nominal_voltage)
        self.power_kw   = float(nominal_kw)
        self._issue_at  = random.randint(15, 70)
        self._scheduled = random.choices(
            [self.ISSUE_VOLT_WARN, self.ISSUE_VOLT_CRIT,
             self.ISSUE_PWR_WARN,  self.ISSUE_PWR_CRIT,
             self.ISSUE_TECH,      self.ISSUE_NONE],
            weights=[20, 8, 20, 8, 6, 38]
        )[0]
        self._active    = self.ISSUE_NONE
        self._fault_msg = ""

    def step(self, idx: int):
        self.voltage  += random.uniform(-3, 3)
        self.power_kw += random.uniform(-0.05, 0.05) * self.nominal_kw
        # FIX: only clamp to normal range when no active fault — otherwise noise
        # on the next tick would restore voltage/power back to normal range and
        # silently undo the injected fault values
        if self._active == self.ISSUE_NONE:
            self.voltage  = max(100, min(self.nominal_v + 20, self.voltage))
            self.power_kw = max(0.1, min(self.nominal_kw * 1.05, self.power_kw))

        if idx == self._issue_at and self._scheduled != self.ISSUE_NONE:
            self._active = self._scheduled
            if   self._active == self.ISSUE_VOLT_WARN: self.voltage  = self.nominal_v * 0.72
            elif self._active == self.ISSUE_VOLT_CRIT: self.voltage  = self.nominal_v * 0.50
            elif self._active == self.ISSUE_PWR_WARN:  self.power_kw = self.nominal_kw * 0.45
            elif self._active == self.ISSUE_PWR_CRIT:  self.power_kw = self.nominal_kw * 0.25
            elif self._active == self.ISSUE_TECH:
                self._fault_msg = random.choice(self._TECH_FAULTS)

        issue, msg = self.ISSUE_NONE, ""
        if   self._active == self.ISSUE_TECH:                      issue, msg = self.ISSUE_TECH,      self._fault_msg
        elif self.voltage  < self.VOLTAGE_CRIT:                    issue, msg = self.ISSUE_VOLT_CRIT, f"Voltage critically low ({self.voltage:.0f}V) — auto stopping!"
        elif self.voltage  < self.VOLTAGE_WARN:                    issue, msg = self.ISSUE_VOLT_WARN, f"Voltage dropping ({self.voltage:.0f}V) — monitor closely"
        elif self.power_kw / self.nominal_kw < self.POWER_CRIT:    issue, msg = self.ISSUE_PWR_CRIT,  f"Power critically low ({self.power_kw:.1f}kW) — auto stopping!"
        elif self.power_kw / self.nominal_kw < self.POWER_WARN:    issue, msg = self.ISSUE_PWR_WARN,  f"Power dip detected ({self.power_kw:.1f}kW) — charging slowed"
        return round(self.voltage, 1), round(self.power_kw, 2), issue, msg

    def is_critical(self, issue: int) -> bool:
        return issue in (self.ISSUE_VOLT_CRIT, self.ISSUE_PWR_CRIT, self.ISSUE_TECH)

    def icon(self, issue: int) -> str:
        if issue == self.ISSUE_NONE: return "🟢"
        if issue in (self.ISSUE_VOLT_WARN, self.ISSUE_PWR_WARN): return "🟡"
        return "🔴"

    def stop_reason(self, issue: int) -> str:
        return {
            self.ISSUE_VOLT_CRIT: "volt_critical",
            self.ISSUE_PWR_CRIT : "power_critical",
            self.ISSUE_TECH     : "tech_fault",
        }.get(issue, "tech_fault")


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 11 │ COOLING WAIT
# ────────────────────────────────────────────────────────────────────────────
# Called when the battery exceeds TEMP_CRITICAL during charging.
# Blocks the charging loop and shows a live cool-down animation until
# TempSimulator.temp drops back to TEMP_RESUME (45°C).
# Each iteration calls temp_sim.cool_down() (drops temp by 0.5–1.5°C),
# then redraws the temperature bar and elapsed pause time in-place.
# Returns the final temperature once cool enough to resume.
# ════════════════════════════════════════════════════════════════════════════

def _wait_for_cooldown(temp_sim: TempSimulator) -> float:
    print(f"\n  OVERHEAT PAUSE — Waiting for battery to cool down...")
    print(f"  Charging paused. Will auto-resume when temp ≤ {TEMP_RESUME}°C\n")
    print("")
    print("")

    pause_start = time.time()
    while temp_sim.temp > TEMP_RESUME:
        cur     = temp_sim.cool_down()
        elapsed = int(time.time() - pause_start)
        # FIX: cursor-up is now part of the redraw block — no stray upward
        # jump when the while condition becomes false after the last iteration
        sys.stdout.write(
            f"\033[2A"
            f"\r  Cooling...  {cur:>5.1f}°C  [{temp_sim.bar(cur, 22)}]  {temp_sim.status(cur)}\n"
            f"\r  Paused for  {elapsed}s  |  Resume at ≤ {TEMP_RESUME}°C\n"
        )
        sys.stdout.flush()
        time.sleep(0.6)

    print(f"\n  Battery cooled to {temp_sim.temp:.1f}°C — resuming charging!\n")
    time.sleep(0.5)
    return temp_sim.temp


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 12 │ DUAL PROGRESS BAR  (charge + temperature + power monitor)
# ────────────────────────────────────────────────────────────────────────────
# Drives the live three-line terminal animation during a charge session:
#   Line 1 – charge progress bar  (0% → total_pct)
#   Line 2 – battery temperature bar + status label
#   Line 3 – live power (kW) and voltage readings from PowerMonitor
#
# Parameters:
#   total_pct       – percentage points to charge  (target_soc - start_soc)
#   label           – display label for the progress bar header
#   temp_sim        – TempSimulator instance for this session
#   delay           – base seconds per tick  (DC=0.15s, AC=0.25s)
#   throttle        – speed multiplier from _THROTTLE_MAP; divides delay
#   nominal_voltage – charger supply voltage passed to PowerMonitor
#   nominal_kw      – charger output kW passed to PowerMonitor
#   start_soc       – battery % at session start (for auto charge-off check)
#   target_soc      – battery % the user wants to reach (auto charge-off target)
#
# Stop conditions (checked in this order each tick):
#   user_stop   – user pressed Enter; background thread sets stop_flag
#   overheat    – TEMP_CRITICAL exceeded >3 times; permanent stop
#   volt/power  – critical PowerMonitor fault; immediate auto-stop
#   full_charge – start_soc + i >= target_soc; auto charge-off
#
# Returns (final_temp, pct_done, stop_reason) to _do_charge().
# ════════════════════════════════════════════════════════════════════════════

def dual_bar(total_pct: int, label: str, temp_sim: TempSimulator,
             delay: float = 0.05, throttle: float = 1.0,
             nominal_voltage: int = 230, nominal_kw: float = 7.2,
             start_soc: int = 0, target_soc: int = 100):

    if total_pct <= 0:
        return temp_sim.temp, 0, 'complete'

    adjusted_delay      = delay / throttle
    BAR_W               = 24
    final_temp          = temp_sim.temp
    temp_log            = []
    stop_flag           = threading.Event()
    pct_done            = 0
    stop_reason         = 'complete'
    full_charge_reached = False
    power_mon           = PowerMonitor(nominal_voltage, nominal_kw)
    alert_shown         = False
    temp_warn_shown     = False
    overheat_count      = 0
    MAX_OVERHEAT_PAUSES = 3

    # Background thread: listens for Enter key → emergency stop
    def _listen():
        try:
            while not stop_flag.is_set():
                sys.stdin.readline()
                if not stop_flag.is_set():
                    stop_flag.set()
                    break
        except Exception:
            pass

    threading.Thread(target=_listen, daemon=True).start()

    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    print(f"  Emergency stop anytime →  just press  Enter\n")
    print(""); print(""); print("")

    try:
        i = 0
        while i <= total_pct:
            if stop_flag.is_set():
                pct_done    = i
                stop_reason = 'user_stop'
                break

            progress   = i / total_pct if total_pct > 0 else 1.0
            cur_temp   = temp_sim.step(progress)
            final_temp = cur_temp
            temp_log.append(cur_temp)
            pct_done   = i

            volt, pw, issue, issue_msg = power_mon.step(i)
            filled_c = int(progress * BAR_W)
            bar_c    = "█" * filled_c + "░" * (BAR_W - filled_c)

            sys.stdout.write("\033[3A")
            sys.stdout.write(f"\r  {label:<20} [{bar_c}] {i:>3}/{total_pct}%\n")
            sys.stdout.write(f"\r  Temp {cur_temp:>5.1f}°C         [{temp_sim.bar(cur_temp, BAR_W)}] {temp_sim.status(cur_temp)}\n")
            sys.stdout.write(f"\r  {power_mon.icon(issue)} Power {pw:>5.1f}kW   Voltage {volt:>5.0f}V   \n")
            sys.stdout.flush()

            # ── Overheat: pause to cool down, resume up to 3 times ──────────────
            if cur_temp >= TEMP_CRITICAL:
                overheat_count += 1
                if overheat_count > MAX_OVERHEAT_PAUSES:
                    print(f"\n  🔴 Battery overheated {overheat_count} times — stopping permanently!")
                    print(f"  Please let your battery rest before charging again.\n")
                    pct_done    = i
                    stop_reason = 'overheat'
                    stop_flag.set()
                    break

                _wait_for_cooldown(temp_sim)

                if stop_flag.is_set():
                    pct_done    = i
                    stop_reason = 'user_stop'
                    break

                remaining = MAX_OVERHEAT_PAUSES - overheat_count
                print(f"  Overheat #{overheat_count} handled. "
                      f"  {'Charging resumed normally.' if remaining > 0 else 'Last chance before permanent stop!'}")
                if remaining > 0:
                    print(f"  {remaining} more overheat pause(s) allowed before permanent stop.\n")
                else:
                    print()
                print(""); print(""); print("")
                i += 1
                continue

            # ── Soft temperature warning: show once then continue ────────────────
            elif cur_temp >= TEMP_WARN and not temp_warn_shown:
                temp_warn_shown = True
                print(f"\n  WARNING: Temp {cur_temp:.1f}°C — getting hot! Monitoring closely...\n")
                print(""); print(""); print("")

            # ── Power / voltage fault handling ───────────────────────────────────
            if issue != power_mon.ISSUE_NONE and not alert_shown:
                alert_shown = True
                if power_mon.is_critical(issue):
                    stop_reason = power_mon.stop_reason(issue)
                    print(f"\n  🔴 CRITICAL: {issue_msg}")
                    print(f"  Charging auto-stopped for safety!\n")
                    pct_done = i
                    stop_flag.set()
                    break
                else:
                    print(f"\n  WARNING: {issue_msg}\n")
                    print(""); print(""); print("")

            # ── Auto charge-off: target SoC reached ─────────────────────────────
            if start_soc + i >= target_soc and not full_charge_reached:
                full_charge_reached = True
                pct_done    = i
                stop_reason = 'full_charge'
                sys.stdout.write("\033[3A")
                sys.stdout.write(f"\r  {label:<20} [{'█' * BAR_W}] {total_pct:>3}/{total_pct}%\n")
                sys.stdout.write(f"\r  Temp {cur_temp:>5.1f}°C         [{temp_sim.bar(cur_temp, BAR_W)}] {temp_sim.status(cur_temp)}\n")
                sys.stdout.write(f"\r  Target {target_soc}% reached — Auto charge-off activated!    \n")
                sys.stdout.flush()
                stop_flag.set()
                break

            time.sleep(adjusted_delay)
            i += 1

    finally:
        stop_flag.set()
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

    print()
    if temp_log:
        peak = max(temp_log)
        avg  = sum(temp_log) / len(temp_log)
        print(f"  Temp  Peak:{peak:.1f}°C  Avg:{avg:.1f}°C  Final:{final_temp:.1f}°C")
        if overheat_count > 0:
            print(f"  Overheat pauses: {overheat_count}")
    print()

    return final_temp, pct_done, stop_reason


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 13 │ RATE RESOLVER
# ────────────────────────────────────────────────────────────────────────────
# Single source of truth for the effective ₹/kWh for any combination of
# ChargeRate × charger type × booking status.
# Looks up the base rate from AC_RATE or DC_RATE using the enum's .name,
# then adds BOOKING_SURCHARGE_PER_KWH (₹2) if the session was pre-booked.
# ════════════════════════════════════════════════════════════════════════════

def resolve_rate(charge_rate: ChargeRate, ctype: str, is_booked: bool = False) -> float:
    base_map = DC_RATE if "DC" in ctype else AC_RATE
    rate     = base_map.get(charge_rate.name, base_map["FULL"])
    if is_booked:
        rate += BOOKING_SURCHARGE_PER_KWH
    return round(rate, 2)


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 14 │ CHARGING ACTION  (_do_charge)
# ────────────────────────────────────────────────────────────────────────────
# Orchestrates a complete charging session from start to finish:
#   1. Validates target > start; bails early if already at target.
#   2. Looks up throttle multiplier and effective ₹/kWh via resolve_rate().
#   3. Prints rate breakdown and estimated cost before charging begins.
#   4. Creates TempSimulator and calls dual_bar() to run the live animation.
#   5. On return, prints the appropriate stop or full-charge message.
#   6. Calls _show_receipt() to print and persist the session summary.
#
# Parameters:
#   start_soc   – battery % at session start
#   target_soc  – battery % the user wants to reach
#   ctype       – "AC" or "DC"
#   charge_rate – ChargeRate enum value assigned by GridDecisionEngine
#   is_booked   – True if session came from a pre-booked slot (adds surcharge)
# ════════════════════════════════════════════════════════════════════════════

def _do_charge(start_soc: int, target_soc: int,
               ctype: str, charge_rate: ChargeRate,
               is_booked: bool = False, state: 'EVState' = None,
               booked_car_id: str = "",
               paid_amount: float = 0.0, refund: float = 0.0):
    req = target_soc - start_soc
    if req <= 0:
        print(f"  Already at {start_soc}%!\n")
        return

    throttle = _THROTTLE_MAP.get(charge_rate.name, 1.0)
    eff_rate = resolve_rate(charge_rate, ctype, is_booked)
    est_cost = round((req / 100) * BATTERY_CAPACITY_KWH * eff_rate, 2)

    if throttle < 1.0:
        print(f"  Charging at {charge_rate.value} due to grid load")
    if is_booked:
        print(f"  Pre-booking surcharge applied: +₹{BOOKING_SURCHARGE_PER_KWH}/kWh")

    base_map = DC_RATE if "DC" in ctype else AC_RATE
    base     = base_map.get(charge_rate.name, base_map["FULL"])
    if is_booked:
        print(f"  Rate: ₹{base}/kWh + ₹{BOOKING_SURCHARGE_PER_KWH} booking = ₹{eff_rate}/kWh")
    else:
        print(f"  Rate: ₹{eff_rate}/kWh  ({charge_rate.value})")
    print(f"  {ctype}  {start_soc}% → {target_soc}%  |  Est. cost: ₹{est_cost}\n")

    temp_sim = TempSimulator(TEMP_AMBIENT, ctype)
    start    = datetime.now()
    nom_kw   = DC_CHARGER_KW if "DC" in ctype else AC_CHARGER_KW
    # FIX: pass actual vehicle voltage instead of hardcoded 230V so
    # PowerMonitor warning/critical thresholds match real vehicle voltage
    nom_v    = state.voltage if state is not None else 230

    final_temp, pct_done, s_reason = dual_bar(
        req, f"{ctype} {start_soc}→{target_soc}%",
        temp_sim,
        delay           = 0.15 if "DC" in ctype else 0.25,
        throttle        = throttle,
        nominal_voltage = nom_v,
        nominal_kw      = nom_kw,
        start_soc       = start_soc,
        target_soc      = target_soc,
    )

    if s_reason == 'full_charge':
        print(f"\n  Target {target_soc}% reached — Charging automatically switched off! \n")
    elif pct_done < req:
        stop_labels = {
            'user_stop'      : " EMERGENCY STOP — Charging halted by user",
            'volt_critical'  : " AUTO STOP — Voltage dropped critically",
            'power_critical' : " AUTO STOP — Power supply critically low",
            'tech_fault'     : " AUTO STOP — Technical fault detected",
            'overheat'       : "️ AUTO STOP — Battery overheated too many times",
        }
        msg = stop_labels.get(s_reason, " Charging stopped")
        print(f"\n  {msg} at ~{start_soc + pct_done}%  (+{pct_done}% charged)\n")

        # ── Grid failure: offer wait-and-retry ──────────────────────────────
        if s_reason in ('power_critical', 'volt_critical'):
            est_restore_min = random.randint(3, 12)   # simulated grid restore ETA
            print(f"{'─'*54}")
            if s_reason == 'power_critical':
                print(f"  Grid power dropped critically low.")
            else:
                print(f"  Grid voltage dropped critically low.")
            print(f"  Estimated grid restore time : ~{est_restore_min} minutes")
            print(f"  Charged so far              : {start_soc + pct_done}%")
            print(f"  Remaining to target         : {target_soc - (start_soc + pct_done)}%")
            print(f"{'─'*54}")
            try:
                ans = input(f"  Wait ~{est_restore_min} min for grid to restore and resume? (Y/N): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                ans = 'n'

            if ans == 'y':
                # Countdown wait animation
                print()
                for remaining in range(est_restore_min * 60, 0, -1):
                    mins_left = remaining // 60
                    secs_left = remaining % 60
                    sys.stdout.write(f"\r  Waiting for grid restore...  {mins_left:02d}:{secs_left:02d} remaining   ")
                    sys.stdout.flush()
                    time.sleep(1)
                print(f"\r  Grid restored! Resuming charge from {start_soc + pct_done}%...        \n")
                time.sleep(0.8)

                # Resume charging from where it stopped
                _do_charge(
                    start_soc     = start_soc + pct_done,
                    target_soc    = target_soc,
                    ctype         = ctype,
                    charge_rate   = charge_rate,
                    is_booked     = is_booked,
                    state         = state,
                    booked_car_id = booked_car_id,
                )
                return   # receipt already printed by the recursive call
            else:
                print(f"\n  Okay, stopping here at {start_soc + pct_done}%. You can resume later.\n")

    _show_receipt(pct_done, eff_rate, ctype, start, final_temp, charge_rate,
                  stop_reason=s_reason, start_soc=start_soc,
                  is_booked=is_booked, target_soc=target_soc,
                  booked_car_id=booked_car_id,
                  paid_amount=paid_amount, refund=refund)


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 15 │ SESSION RECEIPT
# ────────────────────────────────────────────────────────────────────────────
# Prints a formatted end-of-session summary and appends it to HISTORY_FILE.
# Calculates actual kWh delivered and final ₹ cost from pct × rate.
# "Started at / Stopped at" lines appear only for mid-session stops.
# Receipt header tag is dynamic — different emoji per stop reason.
# ════════════════════════════════════════════════════════════════════════════

def _show_receipt(pct: int, rate: float, ctype: str, start: datetime,
                  peak_temp: float, charge_rate: ChargeRate,
                  stop_reason: str = 'complete', start_soc: int = 0,
                  is_booked: bool = False, target_soc: int = 100,
                  booked_car_id: str = "",
                  paid_amount: float = 0.0, refund: float = 0.0):
    end      = datetime.now()
    cost     = round((pct / 100) * BATTERY_CAPACITY_KWH * rate, 2)
    kwh      = round((pct / 100) * BATTERY_CAPACITY_KWH, 2)
    secs     = max(1, int((end - start).total_seconds()))
    mins, secs_rem = divmod(secs, 60)
    stopped  = stop_reason not in ('complete', 'full_charge')

    receipt_tags = {
        'complete'      : "   RECEIPT",
        'full_charge'   : f"  CHARGED TO {target_soc}% — AUTO CHARGE-OFF",
        'user_stop'     : "   EMERGENCY STOP RECEIPT",
        'volt_critical' : "   AUTO STOP — VOLTAGE DROP",
        'power_critical': "   AUTO STOP — POWER FAILURE",
        'tech_fault'    : "   AUTO STOP — TECHNICAL FAULT",
        'overheat'      : "   AUTO STOP — OVERHEATING",
    }

    print(f"{'═'*48}")
    print(receipt_tags.get(stop_reason, "   RECEIPT"))
    print(f"{'─'*48}")
    print(f"  Type        : {ctype}  ({charge_rate.value})")
    print(f"  Rate        : ₹{rate}/kWh" + (" [incl. booking surcharge]" if is_booked else ""))
    if stopped:
        print(f"  Started at  : {start_soc}%")
        print(f"  Stopped at  : ~{start_soc + pct}%")
    print(f"  Charged     : +{pct}%  ({kwh} kWh)")
    print(f"  Duration    : {mins}m {secs_rem}s" if mins > 0 else f"  Duration    : {secs_rem}s")
    print(f"  Peak Temp   : {peak_temp:.1f}°C")
    print(f"  Cost        : ₹{cost}")
    if paid_amount > 0:
        print(f"  Paid        : ₹{paid_amount}")
        if refund > 0:
            print(f"  Refund      : ₹{refund}  ← will be returned to your account")
        else:
            print(f"  Refund      : ₹0.00  (exact charge)")
    print(f"{'═'*48}\n")

    _save_history({
        "date"        : end.strftime("%Y-%m-%d %H:%M"),
        "type"        : ctype,
        "charged_pct" : pct,
        "kwh"         : kwh,
        "cost"        : cost,
        "peak_temp_c" : peak_temp,
        "grid_rate"   : charge_rate.value,
        "stop_reason" : stop_reason,
        "is_booked"   : is_booked,
    })

    # Auto-delete booking record after charging is done (history already saved above)
    if is_booked and booked_car_id:
        _delete_booking_after_charge(booked_car_id)


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 16 │ CHARGING DECISION TREE
# ────────────────────────────────────────────────────────────────────────────
# Rule-based advisor that decides charger type and recommended charge target
# before the user sees the menu.
#
#   decide_charge_type() – recommends DC only when ALL FOUR conditions hold:
#     grid allocated DC  → hardware support confirmed by allocation engine
#     health  ≥ 80%      → battery safe for fast charging
#     voltage > 200V     → DC minimum supply voltage requirement
#     battery ≤ 50%      → DC charging above 50% is harmful to the battery
#     FIX: battery≤50 added — now fully consistent with _run_allocation rules
#
#   decide_recommended_target() – suggests 80% for batteries below 80%
#                                 (preserves longevity) or 100% if ≥ 80%.
# ════════════════════════════════════════════════════════════════════════════

class ChargingDecisionTree:
    def __init__(self, state: EVState, allocation: dict):
        self.state = state
        self.alloc = allocation

    def decide_charge_type(self) -> str:
        s = self.state
        # All four conditions must hold for DC to be safe and viable
        return "DC" if (
            self.alloc.get("charge_type") == "DC"
            and s.health  >= 80
            and s.voltage > 200
            and s.battery <= 50
        ) else "AC"

    def decide_recommended_target(self) -> int:
        return 100 if self.state.battery >= 80 else 80


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 17 │ CHARGER TYPE SUGGESTION
# ────────────────────────────────────────────────────────────────────────────
# Presents the recommended charger type with a plain-English reason, then
# offers the user a chance to switch to the alternate type.
# Safety guardrails on override:
#   → DC with health < 80%   : requires explicit risk confirmation
#   → DC with voltage ≤ 200V : blocked outright (DC physically won't work)
# Returns the final charger type string ("AC" or "DC") to the caller.
# ════════════════════════════════════════════════════════════════════════════

def show_charger_suggestion(state: EVState, recommended_ctype: str) -> str:
    other_ctype = "AC" if recommended_ctype == "DC" else "DC"

    if recommended_ctype == "DC":
        reason = (
            f"  Your battery is at {state.battery}% and health is {state.health}% —\n"
            f"  DC fast charge is a perfect fit for you! \n"
            f"  Charges faster — ₹{DC_RATE['FULL']}/kWh."
        )
    elif state.health < 80:
        reason = (
            f"  Your battery health is {state.health}% — it's a bit sensitive.\n"
            f"  AC slow charging will protect it better. \n"
            f"  Takes a bit longer, but it's safer for your battery health."
        )
    elif state.voltage <= 200:
        reason = (
            f"  Your voltage is {state.voltage}V — a little low for DC.\n"
            f"  AC charger will give you a safe and smooth session. 🟢"
        )
    else:
        reason = (
            f"  Recommending AC charging —\n"
            f"  battery-friendly at ₹{AC_RATE['FULL']}/kWh. \n"
            f"  Slow and steady wins the race! "
        )

    print(f"\n{'─'*48}")
    print(f"   Charger Recommendation")
    print(f"{'─'*48}")
    print(f"  Assigned  : {recommended_ctype} Charging")
    print(f"  Alternate : {other_ctype} Charging")
    print(f"{'─'*48}")
    print(reason)
    print(f"{'─'*48}")

    try:
        ans = input(f"\n  Would you like to switch to {other_ctype} charging instead? (Y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        ans = 'n'

    if ans == 'y':
        if other_ctype == "DC" and state.health < 80:
            print(f"\n  DC fast charge is not safe for your battery right now!")
            print(f"  Health {state.health}% < 80% — DC may degrade your battery. ")
            try:
                confirm = input("  Proceed with DC anyway? At your own risk (Y/N): ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                confirm = 'n'
            if confirm != 'y':
                print(f"  Smart choice! Sticking with AC. \n")
                return recommended_ctype
        elif other_ctype == "DC" and state.voltage <= 200:
            print(f"\n  Voltage {state.voltage}V — DC charger won't work properly here. AC is safer! \n")
            return recommended_ctype
        print(f"\n  Got it! Switching to {other_ctype} charging. \n")
        return other_ctype

    print(f"\n  Starting with {recommended_ctype} charging! \n")
    return recommended_ctype


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 18 │ USER MENU — charging mode selection
# ────────────────────────────────────────────────────────────────────────────
# Displays five charging options and returns an action dict to execute_action:
#   1. till80          – charge from current % to 80%  (battery-friendly default)
#   2. schedule_prebook– open the slot booking flow
#   3. amount          – user enters ₹ amount; system calculates reachable %
#                        FIX: uses actual assigned charge_rate (not always FULL)
#                        so the ₹→% conversion is accurate for throttled/slow
#   4. bytime          – user enters minutes; estimates % gain
#                        (DC ≈ 2%/min, AC ≈ 1%/min)
#   5. till100         – full charge to 100%
# ════════════════════════════════════════════════════════════════════════════

def show_menu(state: EVState, ctype: str, alloc: dict, recommended_target: int, is_booked: bool = False) -> Optional[dict]:
    rate = alloc.get("rate", ChargeRate.FULL)
    print(f"  SELECT CHARGING MODE  ({rate.value})\n")
    print(f"  1. Charge to 100%          Full charge")
    print(f"  2. Charge till 80%         Recommended")
    print(f"  3. Top-up by amount        ₹ based")
    print(f"  4. Charge by time          Duration based")
    print()

    try:
        choice = input("  Your choice (1-4): ").strip()
    except (KeyboardInterrupt, EOFError):
        return None

    if choice == "1":
        if state.battery >= 100:
            print("  Already at 100%!"); return None
        return {"mode": "till100", "ctype": ctype, "alloc": alloc}

    elif choice == "2":
        return {"mode": "till80", "ctype": ctype, "alloc": alloc}

    elif choice == "3":
        try:
            eff_r          = resolve_rate(rate, ctype, is_booked=is_booked)
            pct_remaining  = 100 - state.battery
            cost_to_full   = round((pct_remaining / 100) * BATTERY_CAPACITY_KWH * eff_r, 2)
            time_to_full_m = pct_remaining // (2 if "DC" in ctype else 1)
            print(f"\n{'─'*48}")
            print(f"   Battery Status")
            print(f"{'─'*48}")
            print(f"  Current charge : {state.battery}%")
            print(f"  To reach 100%  : +{pct_remaining}% needed")
            print(f"  Rate           : ₹{eff_r}/kWh  ({rate.value})" + ("  [incl. booking surcharge]" if is_booked else ""))
            print(f"  Cost to full   : ₹{cost_to_full}")
            print(f"  Time to full   : ~{time_to_full_m} min  ({ctype})")
            print(f"{'─'*48}")
            amt = float(input("  Enter amount (₹): ").strip())
            if amt <= 0: raise ValueError
            pct   = min(pct_remaining, int((amt / eff_r / BATTERY_CAPACITY_KWH) * 100))
            if pct <= 0:
                print("  Amount too small."); return None
            # actual_cost = what charging pct% truly costs at eff_r
            actual_cost = round((pct / 100) * BATTERY_CAPACITY_KWH * eff_r, 2)
            refund      = round(amt - actual_cost, 2)
            return {"mode": "amount", "ctype": ctype, "alloc": alloc,
                    "pct": pct, "amount": amt, "actual_cost": actual_cost, "refund": refund}
        except ValueError:
            print("  Invalid amount."); return None

    elif choice == "4":
        try:
            ppm            = 2 if "DC" in ctype else 1
            pct_remaining  = 100 - state.battery
            time_to_full_m = pct_remaining // ppm if ppm > 0 else pct_remaining
            eff_r          = resolve_rate(rate, ctype, is_booked=False)
            cost_to_full   = round((pct_remaining / 100) * BATTERY_CAPACITY_KWH * eff_r, 2)
            print(f"\n{'─'*48}")
            print(f"   Charge Time Info")
            print(f"{'─'*48}")
            print(f"  Current charge   : {state.battery}%")
            print(f"  To reach 100%    : +{pct_remaining}% needed")
            print(f"  Charger speed    : ~{ppm}%/min  ({ctype})")
            print(f"  Time to full     : ~{time_to_full_m} min")
            print(f"  Cost for full    : ₹{cost_to_full}")
            print(f"{'─'*48}")
            mins = int(input("  Duration (minutes): ").strip())
            if mins <= 0: raise ValueError
            pct  = min(pct_remaining, mins * ppm)
            # FIX: battery already full → pct=0, nothing to charge
            if pct <= 0:
                print("  Battery is already full — nothing to charge.\n"); return None
            return {"mode": "bytime", "ctype": ctype, "alloc": alloc, "pct": pct, "mins": mins}
        except ValueError:
            print("  Invalid duration."); return None
            
    else:
        print("  Invalid choice."); return None


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 18B │ TILL-100 INTERACTIVE FLOW
# ────────────────────────────────────────────────────────────────────────────
# Called when user picks "Charge to 100%" from the main menu.
# Steps:
#   1. Ask AC or DC preference
#   2. Show ⚡ LIVE GRID STATUS dashboard:
#        - Grid load bar (load%, kW used/available/remaining)
#        - Smart suggestion: best mode based on current grid condition
#        - All 4 power modes listed with [✓ AVAILABLE] / [✗ UNAVAILABLE] tags
#          and [★ SUGGESTED] on the recommended one
#   3. User selects a mode; grid validates if it can support it:
#        FULL      → needs full charger_kw available
#        THROTTLED → needs ≥ charger_kw × 0.5
#        SLOW      → needs > 0 kW (any positive remainder)
#        QUEUED    → always allowed (0 kW, just waiting)
#      If grid can't support chosen mode → show sorry + suggest best available
#   4. Proceed with _do_charge using chosen ctype + ChargeRate
# ════════════════════════════════════════════════════════════════════════════

def _till100_flow(state: EVState, alloc: dict, is_booked: bool = False, booked_car_id: str = ""):
    bat = state.battery
    if bat >= 100:
        print("  Already at 100%!\n"); return

    # ── Step 1: AC or DC ──────────────────────────────────────────────────────
    print(f"\n{'─'*54}")
    print(f"   FULL CHARGE — {bat}% → 100%")
    print(f"{'─'*54}")
    print(f"  Charger type:")
    print(f"  1. AC  (slow, battery-friendly)  ₹{AC_RATE['FULL']}/kWh base")
    print(f"  2. DC  (fast, voltage ≥ 200V)    ₹{DC_RATE['FULL']}/kWh base")
    print(f"{'─'*54}")

    # Safety guard: DC needs voltage > 200 and health >= 80
    dc_blocked = state.voltage <= 200 or state.health < 80
    if dc_blocked:
        reason = "low voltage" if state.voltage <= 200 else f"battery health {state.health}% < 80%"
        print(f"  DC unavailable for your vehicle ({reason}) — AC only.\n")
        chosen_ctype = "AC"
    else:
        try:
            ct = input("  Your choice (1=AC / 2=DC): ").strip()
        except (KeyboardInterrupt, EOFError):
            ct = '1'
        if ct == '2':
            chosen_ctype = "DC"
            print(f"\n  DC fast charge selected.\n")
        else:
            chosen_ctype = "AC"
            print(f"\n  AC slow charge selected.\n")

    charger_kw = DC_CHARGER_KW if chosen_ctype == "DC" else AC_CHARGER_KW

    # ── Step 2: Grid Status Dashboard + Choose charge mode ───────────────────
    # Grid remaining kW = total available − what's already in use
    grid_avail  = alloc.get("_grid_avail_kw", GRID_MAX_KW)   # injected below in execute_action
    grid_used   = alloc.get("_grid_used_kw",  0.0)
    rem_kw      = max(0.0, grid_avail - grid_used)
    load_pct    = min(100, int((grid_used / grid_avail) * 100) if grid_avail > 0 else 100)

    # Grid load bar (10 blocks)
    filled  = load_pct // 10
    load_bar = "█" * filled + "░" * (10 - filled)
    if load_pct >= 90:   grid_icon = "🔴"; grid_status = "CRITICAL"
    elif load_pct >= 60: grid_icon = "🟡"; grid_status = "STRESSED"
    elif load_pct >= 30: grid_icon = "🟢"; grid_status = "MODERATE"
    else:                grid_icon = "🟢"; grid_status = "LOW LOAD"

    # What modes the current grid can physically support
    can_full      = rem_kw >= charger_kw
    can_throttled = rem_kw >= charger_kw * 0.5
    can_slow      = rem_kw > 0
    # QUEUED is always "possible" — user just waits

    # ── Random charger port availability (realistic simulation) ─────────────
    # Each run, randomly decide how many modes are available (1 to 4).
    # Always keep at least 1 available (QUEUED is guaranteed as fallback).
    # Higher modes (FULL, THROTTLED) are more likely to be constrained.
    _all_modes     = ['full', 'throttled', 'slow']  # queued always available
    _num_available = random.randint(0, 3)            # how many of the 3 top modes are up
    _avail_set     = set(random.sample(_all_modes, _num_available))

    # Intersect with actual grid capacity — can't offer FULL if grid can't support it
    port_full      = can_full      and ('full'      in _avail_set)
    port_throttled = can_throttled and ('throttled' in _avail_set)
    port_slow      = can_slow      and ('slow'      in _avail_set)
    # port_queued is always available (it's just waiting — no port hardware needed)

    # Smart suggestion logic based on grid condition + port availability
    if port_full:
        suggested_key = '1'
        suggest_reason = "Grid has plenty of headroom — full speed available"
    elif port_throttled:
        suggested_key = '2'
        suggest_reason = "Grid is stressed — throttled mode is the best available"
    elif port_slow:
        suggested_key = '3'
        suggest_reason = "Grid is heavily loaded — slow charge is the safest option"
    else:
        suggested_key = '4'
        suggest_reason = "No ports free right now — join the queue for next available slot"

    rate_table = DC_RATE if chosen_ctype == "DC" else AC_RATE

    # ── Print Grid Status Dashboard ──────────────────────────────────────────
    print(f"\n{'═'*54}")
    print(f"   ⚡ LIVE GRID STATUS")
    print(f"{'─'*54}")
    print(f"  Grid Load    : [{load_bar}] {load_pct}%  {grid_icon} {grid_status}")
    print(f"  Total Avail  : {grid_avail:.1f} kW")
    print(f"  In Use       : {grid_used:.1f} kW")
    print(f"  Remaining    : {rem_kw:.1f} kW  ← your budget")
    print(f"  Charger Need : {charger_kw:.1f} kW  ({chosen_ctype} @ 100% speed)")
    print(f"{'─'*54}")
    print(f"  💡 Suggestion: {suggest_reason}")
    print(f"{'═'*54}")

    # ── Print Mode Selection Table ────────────────────────────────────────────
    def _mode_line(num, label, speed_pct, kw_need, rate_key, available, is_suggested):
        avail_tag   = "✓  AVAILABLE" if available  else "✗  UNAVAILABLE"
        suggest_tag = "  ★ SUGGESTED" if is_suggested else ""
        return (
            f"  {num}. {label:<11}"
            f"  {speed_pct:>3}% speed"
            f"  {kw_need:>5.1f} kW"
            f"  ₹{rate_table[rate_key]}/kWh"
            f"  [{avail_tag}]{suggest_tag}"
        )

    print(f"\n{'─'*54}")
    print(f"   SELECT POWER MODE")
    print(f"{'─'*54}")
    print(_mode_line('1', "Full",      100, charger_kw,        "FULL",      port_full,      suggested_key == '1'))
    print(_mode_line('2', "Throttled",  75, charger_kw * 0.75, "THROTTLED", port_throttled, suggested_key == '2'))
    print(_mode_line('3', "Slow",       50, charger_kw * 0.50, "SLOW",      port_slow,      suggested_key == '3'))
    print(_mode_line('4', "Queued",      0, 0.0,               "QUEUED",    True,           suggested_key == '4'))
    print(f"{'─'*54}")

    _MODE_MAP = {
        '1': (ChargeRate.FULL,      port_full,      "Full"),
        '2': (ChargeRate.THROTTLED, port_throttled, "Throttled (75%)"),
        '3': (ChargeRate.SLOW,      port_slow,      "Slow (50%)"),
        '4': (ChargeRate.QUEUED,    True,           "Queued"),
    }

    try:
        mc = input("  Your choice (1-4): ").strip()
    except (KeyboardInterrupt, EOFError):
        mc = '4'

    if mc not in _MODE_MAP:
        print("  Invalid choice — defaulting to Queued.\n")
        mc = '4'

    chosen_rate, grid_ok, mode_name = _MODE_MAP[mc]

    # ── Step 3: Grid validation ───────────────────────────────────────────────
    if not grid_ok:
        # Figure out the best available mode to suggest
        if port_slow:
            suggested_rate, suggested_name = ChargeRate.SLOW, "Slow (50%)"
        else:
            suggested_rate, suggested_name = ChargeRate.QUEUED, "Queued (wait)"

        rate_table = DC_RATE if chosen_ctype == "DC" else AC_RATE
        print(f"\n{'═'*54}")
        print(f"  Sorry! Grid doesn't have enough power for {mode_name} right now.")
        print(f"  Required : {charger_kw if mc=='1' else charger_kw*0.5:.1f} kW")
        print(f"  Available: {rem_kw:.1f} kW")
        print(f"{'─'*54}")
        print(f"  Best available mode right now: {suggested_name}")
        print(f"  Rate : ₹{rate_table[suggested_rate.name]}/kWh")
        if suggested_rate == ChargeRate.SLOW:
            print(f"  Power: {min(charger_kw*0.5, rem_kw):.1f} kW  (50% speed)")
        else:
            print(f"  Power: 0 kW  (waiting for a slot to free up)")
        print(f"{'═'*54}")

        try:
            ans = input(f"  Proceed with {suggested_name} instead? (Y/N): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            ans = 'n'

        if ans != 'y':
            print(f"\n  Okay, cancelling full charge. Returning to menu.\n")
            return
        chosen_rate = suggested_rate
        print(f"\n  Proceeding with {suggested_name}.\n")

    # ── Step 4: Charge! ───────────────────────────────────────────────────────
    print(f"\n  Full Charge | {bat}% → 100%  [{chosen_rate.value}]\n")
    _do_charge(bat, 100, chosen_ctype, chosen_rate, is_booked, state, booked_car_id)


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 19 │ ACTION EXECUTOR
# ────────────────────────────────────────────────────────────────────────────
# Receives the action dict from show_menu and dispatches to the right handler:
#   till80          → _do_charge(bat, 80, ...)
#   till100         → _do_charge(bat, 100, ...)
#   schedule_prebook→ pre_book_slot(...)
#   amount          → _do_charge(bat, bat+pct, ...)  pct from ₹ calculation
#   bytime          → _do_charge(bat, bat+pct, ...)  pct from time calculation
# Guards against charging when already at or above the target.
# ════════════════════════════════════════════════════════════════════════════

def execute_action(state: EVState, action: dict, is_booked: bool = False,
                   grid: 'GridDecisionEngine' = None, booked_car_id: str = ""):
    if action is None:
        return

    ctype = action["ctype"]
    alloc = action["alloc"]
    rate  = alloc.get("rate", ChargeRate.FULL)
    bat   = state.battery
    mode  = action["mode"]

    # Inject live grid kW info into alloc so _till100_flow can use it
    if grid is not None:
        alloc["_grid_avail_kw"] = grid._available_grid_kw()
        alloc["_grid_used_kw"]  = grid._grid_used_kw

    if mode == "till80":
        if bat >= 80:
            print("  Already at or above 80%!\n"); return
        print(f"\n  Charge till 80% | {bat}% → 80%\n")
        _do_charge(bat, 80, ctype, rate, is_booked, state, booked_car_id)

    elif mode == "till100":
        _till100_flow(state, alloc, is_booked, booked_car_id)

    elif mode == "schedule_prebook":
        pre_book_slot(state, ctype, alloc, my_vid=alloc.get("_my_vid", ""))

    elif mode == "amount":
        pct         = action["pct"]
        amt         = action["amount"]
        actual_cost = action.get("actual_cost", amt)
        refund      = action.get("refund", 0.0)
        print(f"\n  Top-up ₹{amt} → ~{pct}% | {bat}% → {bat+pct}%")
        if refund > 0:
            print(f"  Paid: ₹{amt}  |  Est. charge cost: ₹{actual_cost}  |  Refund: ₹{refund}")
        print()
        _do_charge(bat, bat + pct, ctype, rate, is_booked, state, booked_car_id,
                   paid_amount=amt, refund=refund)

    elif mode == "bytime":
        pct = action["pct"]; mins = action["mins"]
        print(f"\n    Charge {mins} min → ~{pct}% | {bat}% → {bat+pct}%\n")
        _do_charge(bat, bat + pct, ctype, rate, is_booked, state, booked_car_id)


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 20 │ CHARGING HISTORY  (JSON-lines persistent log)
# ────────────────────────────────────────────────────────────────────────────
# Every completed session is appended as one JSON line to HISTORY_FILE.
# Append-only write is O(1) — the full file is never rewritten.
# Reading rebuilds the full list line-by-line so a single corrupted entry
# doesn't break the entire history.
# ════════════════════════════════════════════════════════════════════════════

def _load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    records = []
    with open(HISTORY_FILE) as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    # FIX: skip corrupted lines instead of crashing
                    pass
    return records

def _save_history(entry: dict):
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 21 │ SLOT BOOKING HELPERS  (file-backed JSON store)
# ────────────────────────────────────────────────────────────────────────────
# All booking records live in a single JSON array in BOOKING_FILE.
#
#   _load_bookings()              – reads the full array; returns [] if absent.
#   _save_bookings()              – overwrites the file with the updated array.
#   _expire_bookings()            – marks stale "waiting" bookings as "cancelled"
#                                   once grace period passes; returns only active ones.
#   _is_slot_taken()              – returns conflicting booking if one exists within
#                                   30 minutes of the requested time.
#   _find_booking_by_car()        – looks up active booking by Car ID (case-insensitive).
#   _add_booking()                – creates a new booking record and saves it.
#   _complete_booking()           – marks a booking "completed" on check-in.
#   _delete_booking_after_charge()– permanently removes the booking record after
#                                   charging is done; history already saved by receipt.
# ════════════════════════════════════════════════════════════════════════════

def _load_bookings() -> list:
    if os.path.exists(BOOKING_FILE):
        try:
            with open(BOOKING_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            # FIX: corrupted BOOKING_FILE → return empty instead of crashing
            print("  Booking file appears corrupted — starting with empty bookings.")
            return []
    return []

def _save_bookings(bookings: list):
    with open(BOOKING_FILE, "w") as f:
        json.dump(bookings, f, indent=2)

def _expire_bookings() -> list:
    now      = datetime.now()
    bookings = _load_bookings()
    active   = []
    changed  = False
    for b in bookings:
        sched_dt = datetime.strptime(b["scheduled_time"], "%Y-%m-%d %H:%M")
        deadline = sched_dt + timedelta(minutes=GRACE_PERIOD_MIN)
        if b["status"] == "waiting" and now > deadline:
           b["status"]        = "cancelled"
           b["cancel_reason"] = "No show — grace period expired"
            # FIX: flag that this car_id had a booking but it expired
           b["_expired_flag"] = True
           changed            = True
        if b["status"] == "waiting":
            active.append(b)
    if changed:
        _save_bookings(bookings)
    return active


def _check_expired_booking(car_id: str) -> bool:
    """Returns True if this car_id had a booking that was cancelled due to grace period."""
    car_id_u = car_id.strip().upper()
    if not os.path.exists(BOOKING_FILE):
        return False
    with open(BOOKING_FILE) as f:
        bookings = json.load(f)
    for b in bookings:
        if (b["car_id"].upper() == car_id_u
                and b["status"] == "cancelled"
                and b.get("_expired_flag")):
            return True
    return False

def _is_slot_taken(sched_dt: datetime) -> Optional[dict]:
    for b in _expire_bookings():
        b_dt = datetime.strptime(b["scheduled_time"], "%Y-%m-%d %H:%M")
        if abs((b_dt - sched_dt).total_seconds()) < 30 * 60:
            return b
    return None

def _find_booking_by_car(car_id: str) -> Optional[dict]:
    car_id_u = car_id.strip().upper()
    for b in _expire_bookings():
        if b["car_id"].upper() == car_id_u:
            return b
    return None

def _add_booking(name: str, car_id: str, sched_dt: datetime,
                 ctype: str, car_details: dict) -> dict:
    bookings = _load_bookings()
    booking  = {
        "name"           : name,
        "car_id"         : car_id.upper(),
        "scheduled_time" : sched_dt.strftime("%Y-%m-%d %H:%M"),
        "charge_type"    : ctype,
        "status"         : "waiting",
        "booked_at"      : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cancel_reason"  : "",
        "car_details"    : car_details,
    }
    bookings.append(booking)
    _save_bookings(bookings)
    return booking

def _complete_booking(car_id: str):
    bookings = _load_bookings()
    for b in bookings:
        if b["car_id"].upper() == car_id.upper() and b["status"] == "waiting":
            b["status"] = "completed"
            break
    _save_bookings(bookings)

def _delete_booking_after_charge(car_id: str):
    """Remove the booking entry entirely from BOOKING_FILE after charging is done.
    History is already saved by _show_receipt — booking record is no longer needed."""
    bookings = _load_bookings()
    updated  = [b for b in bookings if b["car_id"].upper() != car_id.upper()]
    if len(updated) < len(bookings):
        _save_bookings(updated)
        print(f"  Booking record for {car_id.upper()} cleared from booking file.\n")


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 22 │ PRE-BOOK FLOW WITH SMART SLOT SUGGESTIONS
# ────────────────────────────────────────────────────────────────────────────
# Lets the user reserve a guaranteed charging slot for a future time.
# Flow:
#   1. Builds a temporary GridDecisionEngine via __new__ (bypasses __init__
#      to avoid a second _simulate_station_load on the live engine), then
#      manually seeds vehicles + runs allocation to project future grid load.
#   2. Calls suggest_free_slots(6) → six 30-min windows ranked
#      emptiest (cheapest) → busiest (most expensive).
#   3. Displays windows with expected AC/DC rates including
#      BOOKING_SURCHARGE_PER_KWH (₹2/kWh) on top of the base rate.
#   4. Collects name, Car ID, and preferred time from the user.
#   5. Validates time, checks 30-min conflict window, matches chosen time
#      to nearest suggested window via next() for expected rate, then saves.
# ════════════════════════════════════════════════════════════════════════════

def pre_book_slot(state: EVState, ctype: str, alloc: dict, my_vid: str = "") -> Optional[dict]:
    print(f"\n{'─'*54}")
    print(f"    PRE-BOOK A CHARGING SLOT")
    print(f"{'─'*54}")
    print(f"  • Slot reserved exclusively for your Car ID.")
    print(f"  • Grace period: {GRACE_PERIOD_MIN} min — after that, auto-cancel.")
    print(f"  • Booking surcharge: +₹{BOOKING_SURCHARGE_PER_KWH}/kWh on standard rate.\n")

    # Temporary engine to project load without touching the live engine
    grid_temp               = GridDecisionEngine.__new__(GridDecisionEngine)
    grid_temp.grid_voltage  = state.voltage
    grid_temp.vehicles      = []
    grid_temp.allocations   = {}
    grid_temp._grid_used_kw = 0.0
    grid_temp._simulate_station_load()
    grid_temp._run_allocation()

    suggestions = grid_temp.suggest_free_slots(num_slots=6)

    print(f"   Suggested Booking Windows (based on current grid load):")
    print(f"  {'─'*50}")
    print(f"  {'#':<4} {'Time Window':<22} {'Load':>5}   {'Charge Speed'}")
    print(f"  {'─'*50}")

    for idx, s in enumerate(suggestions, 1):
        t_start = s["window_start"].strftime("%H:%M")
        t_end   = s["window_end"].strftime("%H:%M")
        r_ac    = AC_RATE.get(s["charge_key"], AC_RATE["FULL"]) + BOOKING_SURCHARGE_PER_KWH
        r_dc    = DC_RATE.get(s["charge_key"], DC_RATE["FULL"]) + BOOKING_SURCHARGE_PER_KWH
        print(f"  {idx:<4} {t_start}–{t_end}          {s['load_pct']:>3}%   {s['rate_label']}")
        print(f"       AC: ₹{r_ac:.1f}/kWh  |  DC: ₹{r_dc:.1f}/kWh\n")

    print(f"  {'─'*50}")
    print(f"  You can enter any of these times or a custom time.\n")

    try:
        name  = input("  Your name   : ").strip()
        t_inp = input("  Time (HH:MM): ").strip()
    except (KeyboardInterrupt, EOFError):
        return None

    if not name:
        print("  Name cannot be empty."); return None

    # Auto-generate a unique Car ID in EV-XX format (EV- + 2 digit number)
    existing_ids = {b["car_id"] for b in _load_bookings()}
    while True:
        car_id = f"EV-{random.randint(10, 99)}"
        if car_id not in existing_ids:
            break
    print(f"  Car ID      : {car_id}  (auto-generated — save this for check-in!)")

    try:
        now      = datetime.now()
        sched_dt = datetime.strptime(t_inp, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        if sched_dt <= now:
            sched_dt += timedelta(days=1)
    except ValueError:
        print("  Invalid time. Use HH:MM"); return None

    if _is_slot_taken(sched_dt):
        print(f"\n  Slot taken — choose a different time (30 min gap).\n"); return None

    # Match chosen time to nearest suggested window for expected rate
    matched_key = next(
        (s["charge_key"] for s in suggestions if s["window_start"] <= sched_dt < s["window_end"]),
        "FULL"
    )
    booked_ac_rate = AC_RATE.get(matched_key, AC_RATE["FULL"]) + BOOKING_SURCHARGE_PER_KWH
    booked_dc_rate = DC_RATE.get(matched_key, DC_RATE["FULL"]) + BOOKING_SURCHARGE_PER_KWH

    booking  = _add_booking(name, car_id, sched_dt, ctype,
                            {"battery": state.battery, "health": state.health,
                             "voltage": state.voltage})
    # FIX: refresh now after user inputs so wait_min is accurate
    wait_min = max(0, int((sched_dt - datetime.now()).total_seconds() // 60))

    print(f"\n{'═'*54}")
    print(f"  SLOT BOOKED!")
    print(f"  Name    : {name}  |  Car ID : {car_id}")
    print(f"  Time    : {sched_dt.strftime('%H:%M  (%d %b %Y)')}  (~{wait_min} min away)")
    print(f"  Charger : {ctype}  |  Grace  : {GRACE_PERIOD_MIN} min")
    print(f"  Expected rate at that time:")
    print(f"  AC → ₹{booked_ac_rate:.1f}/kWh  |  DC → ₹{booked_dc_rate:.1f}/kWh")
    print(f"  (includes ₹{BOOKING_SURCHARGE_PER_KWH:.1f} booking surcharge)")
    print(f"{'═'*54}\n")
    return booking


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 23 │ SPINNER ANIMATION
# ────────────────────────────────────────────────────────────────────────────
# Displays a Braille spinner in-place for `duration` seconds to simulate
# a vehicle data fetch. Hides the terminal cursor while running and restores
# it in the finally block so Ctrl-C never leaves a missing cursor.
# ════════════════════════════════════════════════════════════════════════════

def fetching_animation(label: str = "Fetching vehicle data", duration: float = 1.8):
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    end_t  = time.time() + duration
    i      = 0
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        while time.time() < end_t:
            sys.stdout.write(f"\r  {frames[i % len(frames)]}  {label}...")
            sys.stdout.flush()
            time.sleep(0.1)
            i += 1
        sys.stdout.write(f"\r   {label}... Done!        \n")
        sys.stdout.flush()
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


# ════════════════════════════════════════════════════════════════════════════
# BLOCK 24 │ MAIN — program entry point
# ────────────────────────────────────────────────────────────────────────────
# Orchestrates the full user journey:
#   1. Creates EVState (random battery/health/voltage) and shows welcome.
#   2. Builds GridDecisionEngine; calls _expire_bookings() once at startup.
#   3. Station-full check → offers Wait / Pre-book / Exit.
#   4. Adds the user's vehicle to the grid and shows station status.
#   5. Booking check-in → validates pre-booking, shows diff vs booking time,
#      FIX: pre-booked user now gets the full menu to choose their target
#           instead of being hardcoded to 80%.
#   6. Walk-in flow → optional vehicle details, charger suggestion,
#      menu selection, action execution.
# ════════════════════════════════════════════════════════════════════════════

def main():
    state = EVState()
    state.show_welcome()
    _expire_bookings()

    # ── Step 1: New booking? ──────────────────────────────────────────────────
    try:
        want_book = input("  Do you want to schedule / pre-book a slot? (Y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        want_book = 'n'

    if want_book == 'y':
        grid_temp               = GridDecisionEngine.__new__(GridDecisionEngine)
        grid_temp.grid_voltage  = state.voltage
        grid_temp.vehicles      = []
        grid_temp.allocations   = {}
        grid_temp._grid_used_kw = 0.0
        grid_temp._simulate_station_load()
        grid_temp._run_allocation()
        dt    = ChargingDecisionTree(state, {})
        ctype = dt.decide_charge_type()
        pre_book_slot(state, ctype, {})
        return

    # ── Step 2: Existing booking check ───────────────────────────────────────
    try:
        has_booking = input("  Do you have a pre-booking? (Y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        has_booking = 'n'

    car_id_input     = ''
    booking          = None
    is_valid_booking = False
    if has_booking == 'y':
        try:
            car_id_input = input("  Enter your Car ID: ").strip().upper()
        except (KeyboardInterrupt, EOFError):
            car_id_input = ''
        booking          = _find_booking_by_car(car_id_input) if car_id_input else None
        is_valid_booking = booking is not None
        if not is_valid_booking and car_id_input:
            if _check_expired_booking(car_id_input):
                print(f"\n  Booking for '{car_id_input}' was cancelled — grace period expired.")
                print(f"  Proceeding as walk-in.\n")
            else:
                print(f"\n  No active booking found for '{car_id_input}' — proceeding as walk-in.\n")

    # ── Step 3: Build grid (reserved_slot=True caps simulate at 1 or 2) ──────
    grid = GridDecisionEngine(state.voltage, reserved_slot=is_valid_booking)

    current_count = len(grid.vehicles)
    if current_count >= MAX_VEHICLES:
        grid.show_station_status("__NONE__")
        dt    = ChargingDecisionTree(state, {})
        ctype = dt.decide_charge_type()
        print(f"  Station full ({current_count}/{MAX_VEHICLES}) — no slot available.\n")
        try:
            cont = input("  W = wait  |  B = pre-book  |  Enter = exit: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            cont = ''

        if cont == 'b':
            pre_book_slot(state, ctype, {}); return
        elif cont == 'w':
            for i in range(5, 0, -1):
                print(f"  Slot available in ~{i}s   ", end="\r")
                time.sleep(1)
            print(f"\n   Slot available! Proceeding...\n")
            if len(grid.vehicles) >= MAX_VEHICLES:
                print(f"    Station is still full. Please try again later.\n")
                print(f"  Goodbye! \n"); return
        else:
            print(f"\n  Goodbye! \n"); return

    # ── Step 3: add_vehicle with correct ID, then show grid status ────────────
    vid_override   = car_id_input if is_valid_booking else None
    my_assigned_id = grid.add_vehicle(state, is_booked=is_valid_booking, vid_override=vid_override)

    # Grid status + priority list shown HERE — user's car already in list with correct ID
    grid.show_station_status(my_assigned_id)
    alloc = grid.my_allocation(my_assigned_id)
    alloc["_my_vid"] = my_assigned_id

    # ── Step 4: Booking details / walk-in flow ────────────────────────────────
    if has_booking == 'y':
        booking = _find_booking_by_car(car_id_input) if car_id_input else None

        if booking:
            print(f"\n  Booking found for Car ID: {car_id_input}")
            fetching_animation("Fetching vehicle data", duration=1.8)
            state.show_car_details()
            _complete_booking(car_id_input)

            stored   = booking.get("car_details", {})
            sched_dt = datetime.strptime(booking["scheduled_time"], "%Y-%m-%d %H:%M")
            diff_min = int((sched_dt - datetime.now()).total_seconds() // 60)

            print(f"\n{'═'*48}")
            print(f"   WELCOME BACK, {booking['name'].upper()}! ")
            print(f"{'─'*48}")
            if diff_min > 0:    print(f"  You're {diff_min} min early — perfect!")
            elif diff_min == 0: print(f"  Right on time!")
            else:               print(f"  {abs(diff_min)} min late — but you made it!")
            print(f"  Slot   : {booking['scheduled_time']}")
            print(f"  Car ID : {car_id_input}  |  Status: 🟢 RESERVED")
            print(f"{'─'*48}")

            def _arrow(d): return f"▲+{d}" if d > 0 else (f"▼{d}" if d < 0 else "—")
            bat_d = state.battery - stored.get("battery", state.battery)
            hlt_d = state.health  - stored.get("health",  state.health)
            vlt_d = state.voltage - stored.get("voltage", state.voltage)
            print(f"  {'':12} {'At Booking':>11}   {'Now':>9}   {'Δ':>8}")
            print(f"  {'Battery':<12} {stored.get('battery','?'):>11}%  {state.battery:>9}%  {_arrow(bat_d):>8}")
            print(f"  {'Health':<12} {stored.get('health', '?'):>11}%  {state.health:>9}%  {_arrow(hlt_d):>8}")
            print(f"  {'Voltage':<12} {stored.get('voltage','?'):>11}V  {state.voltage:>9}V  {_arrow(vlt_d):>8}")
            print(f"{'═'*48}\n")

            dt           = ChargingDecisionTree(state, alloc)
            booked_ctype = booking.get("charge_type", dt.decide_charge_type())
            ctype        = show_charger_suggestion(state, booked_ctype)

            # FIX: pre-booked user gets full menu to choose their own target
            print(f"  Starting pre-booked session...\n")
            action = show_menu(state, ctype, alloc, dt.decide_recommended_target(), is_booked=True)
            execute_action(state, action, is_booked=True, grid=grid, booked_car_id=car_id_input)
            return
    # ── Normal walk-in flow ───────────────────────────────────────────────────
    dt    = ChargingDecisionTree(state, alloc)
    ctype = dt.decide_charge_type()

    try:
        ans = input("  View your vehicle details? (Y/N): ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        ans = 'n'
    if ans == 'y':
        fetching_animation("Fetching vehicle data", duration=1.8)
        state.show_car_details()

    ctype  = show_charger_suggestion(state, ctype)
    action = show_menu(state, ctype, alloc, dt.decide_recommended_target(), is_booked=False)
    execute_action(state, action, is_booked=False, grid=grid)


if __name__ == "__main__":
    main()