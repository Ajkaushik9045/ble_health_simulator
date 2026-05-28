"""
SensioVital BLE Peripheral Simulator  v3
=========================================
Root cause fix:
  bless hardcodes `advertisement._service_uuids.append(self.services[0].UUID)`
  meaning only service[0] is ever advertised. Services 2 & 3 exist in GATT
  but are never announced — so scanners and centrals skip them.

Solution: ONE primary service containing all characteristics.
  All 4 vitals chars live under one custom service UUID.
  Standard BT SIG UUIDs (HR 0x2A37, Battery 0x2A19) still used for chars
  so Flutter app parsers work correctly.

  Additionally we monkey-patch start_advertising to add all service UUIDs
  to the advertisement packet.

FLUTTER APP UUID REFERENCE  ← update your constants file:
  Primary Service : 12345678-1234-4678-8234-56789abcdef0
  HR Measurement  : 00002a37-0000-1000-8000-00805f9b34fb  (BT SIG)
  SpO2            : 12345678-1234-4678-8234-56789abcdef1
  Temperature     : 12345678-1234-4678-8234-56789abcdef2
  Battery Level   : 00002a19-0000-1000-8000-00805f9b34fb  (BT SIG)
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
log = logging.getLogger("SensioVital")


# ─────────────────────────────────────────────────────────────
# UUIDs  — single service, all chars inside
# ─────────────────────────────────────────────────────────────
class SvcUUID:
    PRIMARY = "12345678-1234-4678-8234-56789abcdef0"   # one service rules them all


class CharUUID:
    HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"   # BT SIG
    BATTERY_LEVEL  = "00002a19-0000-1000-8000-00805f9b34fb"   # BT SIG
    SPO2           = "12345678-1234-4678-8234-56789abcdef1"
    TEMPERATURE    = "12345678-1234-4678-8234-56789abcdef2"


# ─────────────────────────────────────────────────────────────
# Vitals Engine
# ─────────────────────────────────────────────────────────────
class VitalsEngine:
    def __init__(self):
        self._tick = 0
        self._hr_base = 72.0
        self._spo2 = 97.0
        self._temp = 36.6
        self._battery = 85
        self._last_batt_drain = 0

    def _next_hr(self) -> int:
        wave = 8 * math.sin(self._tick / 30.0)
        noise = random.gauss(0, 1.2)
        self._hr_base += random.gauss(0, 0.05)
        self._hr_base = max(55.0, min(105.0, self._hr_base))
        return max(50, min(120, int(round(self._hr_base + wave + noise))))

    def _next_spo2(self) -> float:
        self._spo2 += random.gauss(0, 0.08)
        self._spo2 = max(94.0, min(99.5, self._spo2))
        return round(self._spo2, 1)

    def _next_temp(self) -> float:
        self._temp += random.gauss(0, 0.01)
        self._temp = max(35.8, min(37.5, self._temp))
        return round(self._temp, 2)

    def _next_battery(self) -> int:
        if self._tick - self._last_batt_drain >= 60:
            self._battery = max(0, self._battery - 1)
            self._last_batt_drain = self._tick
        return self._battery

    @staticmethod
    def encode_hr(v: int) -> bytearray:
        return bytearray([0x00, v])                          # flags=0, uint8 bpm

    @staticmethod
    def encode_spo2(v: float) -> bytearray:
        return bytearray(struct.pack("<H", int(v * 100)))    # uint16 LE ×100

    @staticmethod
    def encode_temp(v: float) -> bytearray:
        return bytearray(struct.pack("<I", int(v * 100)))    # uint32 LE ×100

    @staticmethod
    def encode_battery(v: int) -> bytearray:
        return bytearray([v])

    def tick(self):
        self._tick += 1
        hr   = self._next_hr()
        spo2 = self._next_spo2()
        temp = self._next_temp()
        batt = self._next_battery()
        return (
            hr,   self.encode_hr(hr),
            spo2, self.encode_spo2(spo2),
            temp, self.encode_temp(temp),
            batt, self.encode_battery(batt),
        )


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
# BLE Peripheral Server
# ─────────────────────────────────────────────────────────────
class SensioVitalServer:
    DEVICE_NAME     = "SensioVital"
    UPDATE_INTERVAL = 1.0

    def __init__(self):
        self.engine: VitalsEngine          = VitalsEngine()
        self.server: Optional[BlessServer] = None
        self._running                      = False

    def _on_read(self, characteristic: BlessGATTCharacteristic, **_) -> bytearray:
        uuid = characteristic.uuid
        if uuid == CharUUID.BATTERY_LEVEL:
            return self.engine.encode_battery(self.engine._battery)
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

        # ── Single primary service ─────────────────────────────────────────
        await self.server.add_new_service(SvcUUID.PRIMARY)

        await self.server.add_new_characteristic(
            SvcUUID.PRIMARY, CharUUID.HR_MEASUREMENT, NOTIFY_READ,
            bytearray([0x00, 72]), PERM_READ,
        )
        await self.server.add_new_characteristic(
            SvcUUID.PRIMARY, CharUUID.BATTERY_LEVEL, NOTIFY_READ,
            bytearray([85]), PERM_READ,
        )
        await self.server.add_new_characteristic(
            SvcUUID.PRIMARY, CharUUID.SPO2, NOTIFY_READ,
            bytearray(struct.pack("<H", 9750)), PERM_READ,
        )
        await self.server.add_new_characteristic(
            SvcUUID.PRIMARY, CharUUID.TEMPERATURE, NOTIFY_READ,
            bytearray(struct.pack("<I", 3660)), PERM_READ,
        )

        log.info("GATT table: 1 service, 4 characteristics.")

    def _update(self, char_uuid: str, value: bytearray):
        char = self.server.get_characteristic(char_uuid)
        if char:
            char.value = value
            self.server.update_value(SvcUUID.PRIMARY, char_uuid)

    async def _notify_loop(self):
        log.info("Notify loop started — sending vitals every 1 s …\n")
        while self._running:
            await asyncio.sleep(self.UPDATE_INTERVAL)
            hr, hr_b, spo2, spo2_b, temp, temp_b, batt, batt_b = self.engine.tick()
            log.info(
                f"HR={hr:>3} bpm  |  SpO2={spo2:.1f}%  "
                f"|  Temp={temp:.2f}°C  |  Batt={batt}%"
            )
            try:
                self._update(CharUUID.HR_MEASUREMENT, hr_b)
                self._update(CharUUID.BATTERY_LEVEL,  batt_b)
                self._update(CharUUID.SPO2,           spo2_b)
                self._update(CharUUID.TEMPERATURE,    temp_b)
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
        self._running = True
        await self.server.start()

        log.info(f"✔  Advertising as '{self.DEVICE_NAME}'")
        log.info("   Connect with nRF Connect or your Flutter app.\n")
        log.info("   ── CHAR UUIDs ─────────────────────────────────────")
        log.info(f"   Service    : {SvcUUID.PRIMARY}")
        log.info(f"   HR (0x2A37): {CharUUID.HR_MEASUREMENT}")
        log.info(f"   SpO2       : {CharUUID.SPO2}")
        log.info(f"   Temp       : {CharUUID.TEMPERATURE}")
        log.info(f"   Battery    : {CharUUID.BATTERY_LEVEL}")
        log.info("   ────────────────────────────────────────────────────\n")

        try:
            await self._notify_loop()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            await self.server.stop()
            log.info("Stopped.")


def main():
    server = SensioVitalServer()
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        log.info("Interrupted — goodbye.")


if __name__ == "__main__":
    main()