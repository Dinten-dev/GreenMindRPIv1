import requests
import json
import time
from websocket import create_connection

ws = create_connection("wss://green-mind.ch/api/v1/ws/sensor/4eb2b305-997f-4767-98b4-6c921e5542d6")
print("Connected")
ws.settimeout(5.0)
try:
    for _ in range(5):
        result = ws.recv()
        print("Received: ", result)
except Exception as e:
    print("Exception:", e)
ws.close()
