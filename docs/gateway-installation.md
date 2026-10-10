# Raspberry Pi Gateway installation and maintenance

For Raspberry Pi OS Lite Bookworm, 64-bit, with ARM64 hardware and NetworkManager.
Allow more than 8 GiB free for the default WAV storage reserve, plus software and OS space.
The Gateway is a Python application; this release is not a bootable Raspberry Pi OS image.

## Downloads

[Manual maintenance ZIP](https://github.com/Dinten-dev/GreenMindRPIv1/releases/download/manual-2026-10-10/gateway-maintenance-2026-10-10.zip)
· [Full source archive](https://github.com/Dinten-dev/GreenMindRPIv1/releases/download/manual-2026-10-10/GreenMindRPIv1-source-2026-10-10.tar.gz)
· [SHA256SUMS](https://github.com/Dinten-dev/GreenMindRPIv1/releases/download/manual-2026-10-10/SHA256SUMS)
· [Release](https://github.com/Dinten-dev/GreenMindRPIv1/releases/tag/manual-2026-10-10).

The maintenance candidate is based on the checked 10 October changes. Hardware acceptance is pending.
Inspect the package manifest for the original base and change revision, not a moving branch name.
Check the selected archive with `shasum -a 256` against the corresponding entry in SHA256SUMS.

## Existing Gateway maintenance

The maintenance ZIP contains the patch, four changed runtime files, tests and English instructions.
It contains no installer, agent or OTA action. Downloading it does not update any Pi.
Peter's current Direct-only sensors do not need this update while the office Pi is off.

1. Identify the installed source revision, actual systemd units, application paths and environment.
2. Compare the patch's base revision to that installed revision. Stop and review differences if they do not match.
3. Back up software, configuration, credentials, SQLite databases, queued WAVs and sidecars. Keep credentials private.
4. In a separate checkout at the documented base, run `git apply --check changes.patch`, review the patch and apply it there.
5. Run the Gateway tests and quality checks documented in the [root README](../README.md#development).
6. Arrange a maintenance window with the Pi owner. Keep the previous code available for rollback.
7. Stop the actual Gateway service gracefully, replace only the reviewed application code and restart it once.
8. Confirm unchanged Gateway registration, local reception, completed WAV metadata and acknowledged uploads.
9. Check warnings for an intentionally short recording; missing diagnostics must not block delivery.
10. If acceptance fails, restore the previous code/configuration. Preserve recordings and current queue state throughout rollback.

Do not execute the general `install-gateway.sh` for this maintenance task: it enables an update agent and starts services.
Do not reset/re-provision the Gateway or remove unacknowledged data.
The changed files are source-relative paths; they are not instructions to blindly overwrite a different installed version.

## New Gateway installation

Flash Raspberry Pi OS Lite, enable SSH and prepare a reviewed full 40-hex source revision from the release manifest.
The existing installer explicitly installs/enables both Gateway and desired-state update-agent services.
Use it only when that new-device management setup is intended. It is not an installer-free maintenance path.

```bash
REVISION=replace-with-reviewed-40-hex-source-revision
mkdir GreenMindRPIv1 && cd GreenMindRPIv1
git init
git remote add origin https://github.com/Dinten-dev/GreenMindRPIv1.git
git fetch --depth 1 origin "$REVISION"
git checkout --detach "$REVISION"
less greenmind-gateway/install-gateway.sh
sudo bash greenmind-gateway/install-gateway.sh "$REVISION"
```

Run the installer on the Pi only after reviewing it. It requires an immutable commit;
do not pipe remote scripts into a privileged shell.

## Register Gateway and sensors

1. Read the per-boot setup password from the Pi console or `sudo journalctl -u greenmind-gateway -b`.
2. Join **GreenMind-Gateway-XXXX** and open `http://10.42.0.1`.
3. Enter Wi-Fi credentials and the six-character **Gateway registration code** from the dashboard.
4. Confirm the registered Gateway receives measurements and can upload to the intended cloud endpoint.
5. Generate a separate Gateway sensor code for each sensor/zone.
6. Complete each sensor's local hotspot/web form using [the sensor setup guide](https://github.com/Dinten-dev/GreenMindArdu/blob/main/docs/gateway-sensor.md).

The new sensor/Gateway flow does not use BLE provisioning.
Before pairing new protocol-v3 sensor firmware, confirm the Pi health endpoint advertises versions `[1, 2, 3]`
and `sequence_acknowledgement: true`. Preserve existing sensor registrations during upgrades.

## Verify operation

Observe at least a complete nominal ten-minute recording and its acknowledged upload.
Confirm correct zone, expected samples, recording times and WAV readability.
An incomplete WAV is retained and uploaded with a warning; connectivity alone is not recording completeness.
Keep unacknowledged data for retry. See [maintenance diagnostics](maintenance-2026-10-10.md)
and [runtime reference](../greenmind-gateway/README.md).
