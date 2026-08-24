"""Camera scanning: decoding and sensor-level de-duplication.

Deliberately free of Qt, for the same reason core/ is: this is the part worth
testing, and it must be testable with no display, no camera, and no event loop.
The Qt plumbing that feeds it lives in trackify/ui/camera.py.
"""
