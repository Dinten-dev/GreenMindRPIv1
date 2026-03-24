import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import httpx
import logging
import threading
import os
import signal
from src.core.config_store import SecretStore
from src.network.wifi_manager import NetworkManager

logger = logging.getLogger(__name__)

app = FastAPI(title="GreenMind Gateway Setup")

# Default in-memory reference
store_ref = None
setup_success = False

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>GreenMind Setup</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f5f5f7; color: #1d1d1f; max-width: 400px; margin: 40px auto; padding: 20px; }
        .card { background: white; padding: 30px; border-radius: 18px; box-shadow: 0 4px 24px rgba(0,0,0,0.06); }
        h1 { text-align: center; font-size: 24px; font-weight: 600; margin-bottom: 24px; }
        input { width: 100%; padding: 12px; margin: 8px 0 20px 0; border: 1px solid #d2d2d7; border-radius: 8px; box-sizing: border-box; font-size: 16px; }
        button { width: 100%; padding: 14px; background-color: #0071e3; color: white; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; }
        button:hover { background-color: #0077ed; }
        .error { color: #ff3b30; }
        label { font-size: 14px; font-weight: 500; color: #86868b; }
    </style>
</head>
<body>
    <div class="card">
        <h1>GreenMind Gateway</h1>
        <form method="post" action="/setup">
            <label for="ssid">WiFi Network Name (SSID)</label>
            <input type="text" id="ssid" name="ssid" required placeholder="e.g. MyGreenhouse">
            
            <label for="password">WiFi Password</label>
            <input type="password" id="password" name="password" placeholder="Leave empty if open network">
            
            <label for="pairing_code">Pairing Code (from Dashboard)</label>
            <input type="text" id="pairing_code" name="pairing_code" required placeholder="e.g. A1B2C3D4">
            
            <label for="server_url">Hetzner Server URL (Optional)</label>
            <input type="text" id="server_url" name="server_url" value="https://api.greenmind.xyz/api/v1">

            <button type="submit">Connect to GreenMind</button>
        </form>
    </div>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def get_form():
    return HTML_TEMPLATE

@app.get("/success", response_class=HTMLResponse)
async def get_success():
    return "<h1>Setup Complete! Gateway is rebooting...</h1><p>You can close this window. The Gateway AP will now shut down.</p>"

@app.get("/error", response_class=HTMLResponse)
async def get_error():
    return "<h1>Setup Failed</h1><p>Could not connect to WiFi or Hetzner server. Ensure your pairing code is valid.</p><a href='/'>Try again</a>"

@app.post("/setup")
async def do_setup(
    request: Request,
    ssid: str = Form(...),
    password: str = Form(""),
    pairing_code: str = Form(...),
    server_url: str = Form("https://api.greenmind.xyz/api/v1")
):
    global setup_success
    logger.info(f"Received setup payload for SSID {ssid}. Initiating pairing...")
    
    # 1. Attach to user's wifi
    if not NetworkManager.connect_to_wifi(ssid, password):
        return RedirectResponse(url="/error", status_code=303)
        
    # 2. Assert internet availability
    if not NetworkManager.check_internet():
        logger.error("Connected to WiFi, but no internet.")
        return RedirectResponse(url="/error", status_code=303)
        
    # 3. Request Token from backend
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(f"{server_url}/devices/pair", json={"pairing_code": pairing_code})
            resp.raise_for_status()
            data = resp.json()
            
            device_id = data["device_id"]
            api_key = data["api_key"]
            greenhouse_id = data.get("greenhouse_id", "")
            
            # 4. Save to secure local persistence
            if store_ref:
                store_ref.store_credentials(api_key, device_id, greenhouse_id, server_url)
                setup_success = True
                
                # Signal an exit 2 seconds later so the HTTP request completes elegantly
                threading.Timer(2.0, kill_server).start()
                
                return RedirectResponse(url="/success", status_code=303)
                
    except httpx.HTTPError as e:
        logger.error(f"Hetzner Pairing Failed: {e}")
        # Failure: Bring back the AP so the user can try again
        NetworkManager.start_ap()
        return RedirectResponse(url="/error", status_code=303)

def kill_server():
    """Sends SIGINT to the main thread to terminate the uvicorn loop."""
    logger.info("Terminating setup server...")
    os.kill(os.getpid(), signal.SIGINT)

def run_setup_server(store: SecretStore, port: int = 80) -> bool:
    """Blocks and runs the portal until successful provisioning."""
    global store_ref
    store_ref = store
    logger.info(f"Starting local Setup Portal on 0.0.0.0:{port}")
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    except KeyboardInterrupt:
        logger.info("Uvicorn terminated by KeyboardInterrupt.")
        
    return setup_success
