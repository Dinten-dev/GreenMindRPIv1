# Manual Gateway maintenance candidate, 10 October 2026

Original reviewed change revision: `ae4300d4efefee1f80b89063fccb166e073fed1d`.
The public source manifest identifies the integrated revision. No Pi was installed or restarted by publishing it.

| Change | Effect |
| --- | --- |
| WAV close checks actual/expected sample counts | `WAV_INCOMPLETE` warns about missing seconds and close reason |
| Small JSON sidecar | Records real recording window and close reason |
| Real upload timestamps | Supports reliable new recording windows; older WAVs still upload |
| Deprecated BLE worker no longer starts | Removes obsolete provisioning attempts even if its old flag is set |
| HTTPS-to-WSS and HTTP:443 normalization | Corrects TLS/WebSocket protocol configuration |

A diagnostic sidecar failure does not block WAV completion or upload. Short recordings remain deliverable.
Sampling, PCM conversion and deletion only after explicit upload/hash acknowledgement remain unchanged.
Unacknowledged data is retained and retried.

The cloud completeness display can evaluate old WAVs against their nominal ten-minute window.
Old metadata cannot distinguish every intentional partial file from transport loss; no radio-loss cause is inferred.
The historical office losses still require measurements on the actual ESP/TP-Link/Pi path.
The maintenance change exposes losses but does not establish a radio-link repair.

25 focused checks passed in the original preparation; the published revision is additionally checked through Gateway CI.
Physical Pi/hardware acceptance is pending. Install only after revision review, backup and a maintenance window.
Follow [the installation guide](gateway-installation.md#existing-gateway-maintenance).
