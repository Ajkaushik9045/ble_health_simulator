# BLE Health Simulator

A Bluetooth Low Energy (BLE) peripheral simulator that emulates a health monitoring device with real-time vital signs data.

## Features

- **Heart Rate Monitoring**: Simulates realistic heart rate variations (50-120 bpm) with natural wave patterns
- **SpO2 (Blood Oxygen)**: Maintains oxygen saturation levels between 94-99.5%
- **Temperature**: Simulates body temperature fluctuations (35.8-37.5°C)
- **Battery Level**: Tracks device battery percentage with gradual drain simulation
- **BLE GATT Server**: Full GATT protocol implementation with standard BT SIG UUIDs for compatibility
- **Flutter Integration**: Compatible with Flutter mobile apps using standard BLE characteristic UUIDs

## Requirements

- Python 3.7+
- Linux with BlueZ (Bluetooth stack) installed
- Bluetooth adapter

## Installation

1. Clone or download this project
2. Create a virtual environment (optional but recommended):

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

Run the simulator:

```bash
python simulator.py
```

The simulator will start advertising as a BLE peripheral device and continuously broadcast health metrics:

- **Heart Rate**: Heart rate measurement characteristic (UUID: 00002a37-0000-1000-8000-00805f9b34fb)
- **SpO2**: Custom SpO2 characteristic (UUID: 12345678-1234-4678-8234-56789abcdef1)
- **Temperature**: Custom temperature characteristic (UUID: 12345678-1234-4678-8234-56789abcdef2)
- **Battery Level**: Battery level characteristic (UUID: 00002a19-0000-1000-8000-00805f9b34fb)

## Service UUID

- **Primary Service**: 12345678-1234-4678-8234-56789abcdef0

All health characteristics are contained within this single service UUID.

## Flutter App Integration

Update your Flutter app's UUID constants to match:

```dart
const String primaryServiceUUID = "12345678-1234-4678-8234-56789abcdef0";
const String hrMeasurementUUID = "00002a37-0000-1000-8000-00805f9b34fb";
const String spo2UUID = "12345678-1234-4678-8234-56789abcdef1";
const String temperatureUUID = "12345678-1234-4678-8234-56789abcdef2";
const String batteryLevelUUID = "00002a19-0000-1000-8000-00805f9b34fb";
```

## Technical Details

The simulator uses the `bless` library to create a BLE GATT server with realistic health data generation:

- Heart rate oscillates naturally around a base value with random variations
- SpO2 drifts slightly around 97%
- Temperature maintains body-temperature range
- Battery decreases by 1% every 60 seconds
