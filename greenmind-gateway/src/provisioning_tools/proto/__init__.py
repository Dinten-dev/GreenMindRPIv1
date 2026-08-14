# Modified to load local protobufs
import importlib.util
import os
import sys

proto_dir = os.path.dirname(os.path.abspath(__file__))
if proto_dir not in sys.path:
    sys.path.insert(0, proto_dir)

import constants_pb2
import sec0_pb2
import sec1_pb2
import session_pb2
import wifi_constants_pb2
import wifi_config_pb2
import wifi_scan_pb2
