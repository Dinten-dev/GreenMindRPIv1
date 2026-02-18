# Gateway Authentication & Mapping

The GreenMind Gateway provides flexible options for authenticating devices and mapping station IDs to plant metadata.

## 1. Gateway Authentication (To Mac mini)

The Gateway itself must be authenticated to send data to the Mac mini Backend.

1. Generate a **Device API Key** in the Mac mini Admin Dashboard.
2. Add it to `/etc/greenmind-gateway/config.env`:
   ```env
   DEVICE_API_KEY=your_secret_key_here
   ```
3. Restart the service:
   ```bash
   sudo systemctl restart greenmind-gateway
   ```

If the key is invalid, the Gateway will receive `401 Unauthorized` errors. These requests will be moved to a **Dead Letter Queue (DLQ)** to prevent blocking valid requests. Check `/gw/health` for `dead_letter_depth`.

## 2. ESP32 Authentication (To Gateway)

By default, the Gateway accepts data from any source (`ALLOW_UNAUTHENTICATED_ESP32=true`). To enforce authentication:

1. Set `ALLOW_UNAUTHENTICATED_ESP32=false` in `config.env`.
2. Create/Edit `/etc/greenmind-gateway/esp32_keys.json`:
   ```json
   {
       "station-01": "secret-key-123",
       "station-02": "secret-key-456"
   }
   ```
3. Restart the Gateway.
4. Requests must now include header: `Authorization: Bearer secret-key-123`.

## 3. Station Mapping

The Gateway can automatically inject `plant_id` or other metadata based on the reporting `station_id`. This allows "dumb" sensors to just report their ID.

1. Create/Edit `/etc/greenmind-gateway/station_map.json`:
   ```json
   {
       "station-01": {
           "plant_id": "monstera-deliciosa-01",
           "location": "Living Room"
       },
       "station-02": {
           "plant_id": "ficus-lyrata-02"
       }
   }
   ```
2. Restart the Gateway.
3. When `station-01` sends data, the Gateway will add `"plant_id": "monstera-deliciosa-01"` to the payload before forwarding to the backend.
