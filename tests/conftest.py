"""Test setup: the integration is importable as `custom_components.thingino_cam` (the repo root goes on
sys.path), and the fake RTSP server as `fake_rtsp`."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
