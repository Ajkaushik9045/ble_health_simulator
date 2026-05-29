"""
SensioRing BLE Peripheral Simulator  v1
=========================================
A second BLE peripheral simulating a smart ring wearable.
Run alongside sensio_vital_simulator.py to test multi-device flows.

Exposes ONE primary service with 4 characteristics:
  - HRV / Stress Index     [NOTIFY + READ]  custom
  - Steps Counter          [NOTIFY + READ]  custom
  - Skin Temperature       [NOTIFY + READ]  custom (different from core temp)
  - SpO2 (finger-based)    [NOTIFY + READ]  reuses 0x2A5F (PLX spot-check)

FLUTTER APP UUID REFERENCE:
  Primary Service  : ABCDEF01-1234-4678-8234-56789abcdef0
  HRV / Stress     : ABCDEF01-1234-4678-8234-56789abcdef1
  Steps Counter    : ABCDEF01-1234-4678-8234-56789abcdef2
  Skin Temperature : ABCDEF01-1234-4678-8234-56789abcdef3
  SpO2 PLX         : 00002a5f-0000-1000-8000-00805f9b34fb  (BT SIG PLX)

Device Name : SensioRing
Run         : sudo python sensio_ring_simulator.py
"""

import asyncio
import logging
import math
import random
import struct
from typing import Any, Optional

from bless import (
    BlessGATTCharacteristic,
    BlessServer,
    GATTAttributePermissions,
    GATTCharacteristicProperties,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("SensioRing")


# ─────────────────────────────────────────────────────────────
# UUIDs
# ─────────────────────────────────────────────────────────────
class SvcUUID:
    PRIMARY = "abcdef01-1234-4678-8234-56789abcdef0"


class CharUUID:
    HRV_STRESS  = "abcdef01-1234-4678-8234-56789abcdef1"   # custom
    STEPS       = "abcdef01-1234-4678-8234-56789abcdef2"   # custom
    SKIN_TEMP   = "abcdef01-1234-4678-8234-56789abcdef3"   # custom
    SPO2_PLX    = "00002a5f-0000-1000-8000-00805f9b34fb"   # BT SIG PLX spot-check


# ─────────────────────────────────────────────────────────────
# Monkey-patch: advertise ALL service UUIDs, not just services[0]
# ─────────────────────────────────────────────────────────────
def _patch_advertisement(server: BlessServer):
    """
    bless source (application.py line ~106):
        advertisement._service_uuids.append(self.services[0].UUID)
    We patch start_advertising to append every registered service UUID instead.
    """
    original_start_advertising = server.app.__class__.start_advertising

    async def patched_start_advertising(self, adapter):
        await original_start_advertising(self, adapter)
        if self.advertisements:
            adv = self.advertisements[-1]
            # Replace the single UUID with all service UUIDs
            adv._service_uuids = [svc.UUID for svc in self.services]
            log.info(f"Advertisement UUIDs: {adv._service_uuids}")

    server.app.__class__.start_advertising = patched_start_advertising


# ─────────────────────────────────────────────────────────────
# Ring Vitals Engine
# ─────────────────────────────────────────────────────────────
class RingVitalsEngine:
    """
    HRV (RMSSD ms): 20–80 ms — higher = more relaxed, lower = stressed
    Stress Index  : 0–100    — derived inverse of HRV
    Steps         : monotonic counter, bursts every ~10 ticks (walking simulation)
    Skin Temp     : 31–35°C  — finger skin, lower than core body temp
    SpO2 PLX      : 95–99.5% — finger pulse ox (ring-style)
    """
    def __init__(self):
        self._tick          = 0
        self._hrv           = 45.0    # ms RMSSD
        self._steps         = 0
        self._step_burst    = 0       # steps pending in current walking burst
        self._next_burst_at = random.randint(5, 15)
        self._skin_temp     = 33.2    # °C
        self._spo2          = 97.5    # %

    # ── HRV / Stress ───────────────────────────────────────────
    def _next_hrv(self) -> float:
        # HRV drifts slowly with occasional stress spikes
        self._hrv += random.gauss(0, 0.4)
        self._hrv  = max(20.0, min(80.0, self._hrv))
        return round(self._hrv, 1)

    @staticmethod
    def stress_from_hrv(hrv: float) -> int:
        # Stress 0–100 is inverse of HRV 20–80
        stress = int(round(100 - ((hrv - 20.0) / 60.0) * 100))
        return max(0, min(100, stress))

    @staticmethod
    def encode_hrv_stress(hrv: float, stress: int) -> bytearray:
        # [hrv_uint16_LE (×10), stress_uint8]
        # e.g. hrv=45.3 → 453, stress=42 → [0xC9,0x01,0x2A]
        hrv_raw = int(hrv * 10)
        return bytearray(struct.pack("<HB", hrv_raw, stress))

    # ── Steps ───────────────────────────────────────────────────
    def _next_steps(self) -> int:
        if self._tick >= self._next_burst_at:
            # Start a walking burst of 5–15 steps spread over next few ticks
            if self._step_burst == 0:
                self._step_burst    = random.randint(5, 15)
                self._next_burst_at = self._tick + random.randint(8, 20)
        if self._step_burst > 0:
            # Add 1–2 steps this tick
            delta = min(self._step_burst, random.randint(1, 2))
            self._steps      += delta
            self._step_burst -= delta
        return self._steps

    @staticmethod
    def encode_steps(steps: int) -> bytearray:
        # uint32 LE — supports up to ~4 billion steps
        return bytearray(struct.pack("<I", steps))

    # ── Skin Temperature ────────────────────────────────────────
    def _next_skin_temp(self) -> float:
        self._skin_temp += random.gauss(0, 0.02)
        self._skin_temp  = max(30.0, min(35.5, self._skin_temp))
        return round(self._skin_temp, 2)

    @staticmethod
    def encode_skin_temp(temp: float) -> bytearray:
        # uint32 LE × 100  (33.25 → 3325)
        return bytearray(struct.pack("<I", int(temp * 100)))

    # ── SpO2 PLX (BT SIG 0x2A5F) ───────────────────────────────
    def _next_spo2(self) -> float:
        self._spo2 += random.gauss(0, 0.06)
        self._spo2  = max(95.0, min(99.5, self._spo2))
        return round(self._spo2, 1)

    @staticmethod
    def encode_spo2_plx(spo2: float) -> bytearray:
        """
        BT SIG PLX Spot-Check (0x2A5F) format:
          Byte 0    : flags (0x00 = SpO2 & PR present, no timestamp)
          Bytes 1-2 : SpO2 SFLOAT LE  (IEEE 11073, exponent=0xFF → ×0.1)
          Bytes 3-4 : Pulse Rate SFLOAT LE (we send 0 — ring has no separate PR)
        SFLOAT: upper nibble = exponent (signed 4-bit), lower 12 bits = mantissa
        For ×0.1 scale: exponent=0xF (-1), mantissa = value×10
        """
        def to_sfloat(value_x10: int) -> int:
            # exponent = -1 (0xF in 4-bit signed), mantissa = value×10
            return (0xF << 12) | (value_x10 & 0x0FFF)

        spo2_sf = to_sfloat(int(spo2 * 10))
        pr_sf   = to_sfloat(0)     # pulse rate not available from ring SpO2
        return bytearray(struct.pack("<BHH", 0x00, spo2_sf, pr_sf))

    # ── Tick ────────────────────────────────────────────────────
    def tick(self):
        self._tick += 1
        hrv    = self._next_hrv()
        stress = self.stress_from_hrv(hrv)
        steps  = self._next_steps()
        stemp  = self._next_skin_temp()
        spo2   = self._next_spo2()
        return (
            hrv,   stress, self.encode_hrv_stress(hrv, stress),
            steps,         self.encode_steps(steps),
            stemp,         self.encode_skin_temp(stemp),
            spo2,          self.encode_spo2_plx(spo2),
        )


# ─────────────────────────────────────────────────────────────
# BLE Peripheral Server
# ─────────────────────────────────────────────────────────────
class SensioRingServer:
    DEVICE_NAME     = "SensioRing"
    UPDATE_INTERVAL = 1.0

    def __init__(self):
        self.engine: RingVitalsEngine      = RingVitalsEngine()
        self.server: Optional[BlessServer] = None
        self._running                      = False

    def _on_read(self, characteristic: BlessGATTCharacteristic, **_) -> bytearray:
        return characteristic.value or bytearray()

    def _on_write(self, characteristic: BlessGATTCharacteristic, value: Any, **_):
        log.info(f"WRITE → {characteristic.uuid}  value={list(value)}")

    async def _build_services(self):
        assert self.server

        NOTIFY_READ = (
            GATTCharacteristicProperties.notify
            | GATTCharacteristicProperties.indicate
            | GATTCharacteristicProperties.read
        )
        PERM_READ = GATTAttributePermissions.readable

        await self.server.add_new_service(SvcUUID.PRIMARY)

        await self.server.add_new_characteristic(
            SvcUUID.PRIMARY, CharUUID.HRV_STRESS, NOTIFY_READ,
            bytearray(struct.pack("<HB", 450, 25)),   # 45.0 ms HRV, 25% stress
            PERM_READ,
        )
        await self.server.add_new_characteristic(
            SvcUUID.PRIMARY, CharUUID.STEPS, NOTIFY_READ,
            bytearray(struct.pack("<I", 0)),           # 0 steps
            PERM_READ,
        )
        await self.server.add_new_characteristic(
            SvcUUID.PRIMARY, CharUUID.SKIN_TEMP, NOTIFY_READ,
            bytearray(struct.pack("<I", 3320)),        # 33.20°C
            PERM_READ,
        )
        await self.server.add_new_characteristic(
            SvcUUID.PRIMARY, CharUUID.SPO2_PLX, NOTIFY_READ,
            bytearray(struct.pack("<BHH", 0x00, 0xFBB3, 0xF000)),  # ~97.5%
            PERM_READ,
        )

        log.info("GATT table: 1 service, 4 characteristics.")

    def _update(self, char_uuid: str, value: bytearray):
        char = self.server.get_characteristic(char_uuid)
        if char:
            char.value = value
            self.server.update_value(SvcUUID.PRIMARY, char_uuid)

    async def _notify_loop(self):
        log.info("Notify loop started — sending ring vitals every 1 s …\n")
        while self._running:
            await asyncio.sleep(self.UPDATE_INTERVAL)
            (
                hrv, stress, hrv_b,
                steps,       steps_b,
                stemp,       stemp_b,
                spo2,        spo2_b,
            ) = self.engine.tick()

            log.info(
                f"HRV={hrv:.1f}ms  |  Stress={stress:>3}%  "
                f"|  Steps={steps:>5}  |  SkinT={stemp:.2f}°C  |  SpO2={spo2:.1f}%"
            )

            try:
                self._update(CharUUID.HRV_STRESS, hrv_b)
                self._update(CharUUID.STEPS,      steps_b)
                self._update(CharUUID.SKIN_TEMP,  stemp_b)
                self._update(CharUUID.SPO2_PLX,   spo2_b)
            except Exception as exc:
                log.warning(f"update_value error: {exc}")

    async def run(self):
        log.info(f"Starting {self.DEVICE_NAME} …")

        self.server = BlessServer(
            name=self.DEVICE_NAME,
            loop=asyncio.get_event_loop(),
        )
        self.server.read_request_func  = self._on_read
        self.server.write_request_func = self._on_write

        await self._build_services()
        _patch_advertisement(self.server)
        self._running = True
        await self.server.start()

        log.info(f"✔  Advertising as '{self.DEVICE_NAME}'")
        log.info("   Connect with nRF Connect or your Flutter app.\n")
        log.info("   ── CHAR UUIDs ─────────────────────────────────────────")
        log.info(f"   Service       : {SvcUUID.PRIMARY}")
        log.info(f"   HRV/Stress    : {CharUUID.HRV_STRESS}")
        log.info(f"     └ decode: uint16_LE÷10=HRV(ms), uint8=Stress(0-100)")
        log.info(f"   Steps         : {CharUUID.STEPS}")
        log.info(f"     └ decode: uint32_LE = cumulative step count")
        log.info(f"   Skin Temp     : {CharUUID.SKIN_TEMP}")
        log.info(f"     └ decode: uint32_LE÷100 = °C")
        log.info(f"   SpO2 PLX      : {CharUUID.SPO2_PLX}")
        log.info(f"     └ decode: BT SIG 0x2A5F — byte0=flags, sfloat×0.1=SpO2%")
        log.info("   ────────────────────────────────────────────────────────\n")

        try:
            await self._notify_loop()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            await self.server.stop()
            log.info("Stopped.")


def main():
    server = SensioRingServer()
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        log.info("Interrupted — goodbye.")


if __name__ == "__main__":
    main()