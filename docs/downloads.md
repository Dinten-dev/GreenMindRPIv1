# Downloads and installation

Reviewed downloads as of 10 October 2026. Sensor firmware is installed manually over USB.
The Raspberry Pi package is a manual maintenance candidate with hardware acceptance pending.
Cloud source publication does not deploy a server or activate an archive policy.


| Component | Download | Installation guide |
| --- | --- | --- |
| Direct-to-Cloud sensor, Production v2.8 | [Firmware ZIP](https://github.com/Dinten-dev/GreenMindArdu/releases/download/manual-2026-10-10/direct-production-2.8.zip) | [Direct USB installation](https://github.com/Dinten-dev/GreenMindArdu/blob/main/docs/direct-to-cloud.md) |
| Direct-to-Gateway sensor v1.2.0 | [Firmware ZIP](https://github.com/Dinten-dev/GreenMindArdu/releases/download/manual-2026-10-10/gateway-sensor-1.2.0.zip) | [Gateway sensor USB installation](https://github.com/Dinten-dev/GreenMindArdu/blob/main/docs/gateway-sensor.md) |
| Raspberry Pi Gateway, manual maintenance candidate | [Installer-free maintenance ZIP](https://github.com/Dinten-dev/GreenMindRPIv1/releases/download/manual-2026-10-10/gateway-maintenance-2026-10-10.zip) | [Gateway installation and maintenance](https://github.com/Dinten-dev/GreenMindRPIv1/blob/main/docs/gateway-installation.md) |
| Cloud backend and dashboard, reviewed source snapshot | [Source archive](https://github.com/Dinten-dev/GreenMindDB/releases/download/source-2026-10-10/GreenMindDB-source-2026-10-10.tar.gz) | [Cloud setup](https://github.com/Dinten-dev/GreenMindDB/blob/main/README.md#local-development) and [backend guide](https://github.com/Dinten-dev/GreenMindDB/blob/main/backend/README.md) |


## Verify before installation

- Sensors: download [SHA256SUMS](https://github.com/Dinten-dev/GreenMindArdu/releases/download/manual-2026-10-10/SHA256SUMS) and verify the ZIP, then each binary against its package manifest.
- Raspberry Pi: download [SHA256SUMS](https://github.com/Dinten-dev/GreenMindRPIv1/releases/download/manual-2026-10-10/SHA256SUMS); the maintenance ZIP contains its own manifest and file checksums.
- Cloud: download [SHA256SUMS](https://github.com/Dinten-dev/GreenMindDB/releases/download/source-2026-10-10/SHA256SUMS) and [source manifest](https://github.com/Dinten-dev/GreenMindDB/releases/download/source-2026-10-10/manifest.json). Check the exact source revision before deployment planning.
- Complete manifests record the source revision and validation limitations. Keep each package's binaries and flash addresses together.

## Choose the correct sensor path

Direct-to-Cloud uses a Direct code from the selected cloud dashboard and requires no Pi.
Direct-to-Gateway uses a Gateway sensor code, a sensor-local web form and a registered Pi.
Both sensor forms are available through `GreenMind-Sensor-XXXX` at `http://192.168.4.1`.
Gateway registration uses its own separate dashboard code. These codes are not interchangeable.

For a Staging test only, use the separate [Direct Staging v2.8 ZIP](https://github.com/Dinten-dev/GreenMindArdu/releases/download/manual-2026-10-10/direct-staging-2.8.zip),
the Staging dashboard, and that package's manifest. Never provision Production credentials into Staging.

## Source repositories

- [Sensor firmware](https://github.com/Dinten-dev/GreenMindArdu)
- [Raspberry Pi Gateway](https://github.com/Dinten-dev/GreenMindRPIv1)
- [Cloud API and dashboard](https://github.com/Dinten-dev/GreenMindDB)

The Pi [release page](https://github.com/Dinten-dev/GreenMindRPIv1/releases/tag/manual-2026-10-10)
also offers the reviewed full source archive. Its general installer enables the update agent;
it is not the maintenance procedure for an existing field Gateway.
Older field releases remain historical references. A newer source branch does not replace a checked release binary.
