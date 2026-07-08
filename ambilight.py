import os
import sys
import time
import colorsys
import tinytuya
import mss
import numpy as np
from PIL import Image
from dotenv import load_dotenv
load_dotenv()

# ==========================================
# 1. BULB CONFIGURATION
# Replace these with your actual Device IDs, Local Keys, and Static IPs
# ==========================================

# Wipro Bulb Configuration
WIPRO_IP = os.getenv('WIPRO_IP')          # Replace with Wipro static IP
WIPRO_DEV_ID = os.getenv('WIPRO_DEV_ID') # Replace with Wipro Device ID
WIPRO_LOCAL_KEY = os.getenv('WIPRO_LOCAL_KEY') # Replace with Wipro Local Key
API_VERSION = os.getenv('API_VERSION')

# Halonix Bulb Configuration (Commented out for now)
# HALONIX_IP = "192.168.1.51"
# HALONIX_DEV_ID = "YOUR_HALONIX_DEV_ID"
# HALONIX_LOCAL_KEY = "YOUR_HALONIX_KEY"

# Update frequency (in seconds). 0.1 = 10 frames per second.
# Decrease to 0.05 for faster response, but it may overload cheaper routers.
UPDATE_DELAY = 0.05

# --- Ambilight tuning ---
SMOOTHING = 0.4         # 0..1 — how far toward the new color each frame (lower = smoother, laggier)
SATURATION_BOOST = 2   # >1 counteracts the grey wash of screen averaging
GAMMA = 0.65             # <1 lifts brightness of dark/mid scenes
MIN_BRIGHTNESS = 0.08    # floor so near-black scenes glow dimly instead of going murky
COLOR_DELTA = 4          # skip sending if color changed less than this (reduces flicker/network spam)

# ==========================================
# 2. INITIALIZE BULBS
# ==========================================
def connect_bulb(dev_id, ip, key, name="Bulb"):
    """
    Try each Tuya protocol version until the bulb answers with real data.
    A valid response contains a 'dps' dict; an Error 914 means the
    key/version combo failed to decrypt the handshake.
    """
    version = API_VERSION
    bulb = tinytuya.BulbDevice(dev_id, ip, key, version=3.1)
    bulb.set_socketPersistent(True)
    status = bulb.status()
    if status and "dps" in status:
        print(f"✅ {name} connected (protocol {version})")
        print(f"   Status: {status}")
        return bulb
    print(f"   Protocol {version} failed: {status}")
    bulb.close()

print("Connecting to bulbs locally...")
wipro_bulb = connect_bulb(WIPRO_DEV_ID, WIPRO_IP, WIPRO_LOCAL_KEY, "Wipro Bulb")

if wipro_bulb is None:
    print(
        "\n❌ Handshake failed on every protocol version.\n"
        "   Your LOCAL KEY is almost certainly stale. Tuya rotates the local\n"
        "   key every time a device is removed/re-paired in the Smart Life app.\n"
        "   Fix: re-fetch the key by running:\n"
        "       python -m tinytuya wizard\n"
        "   then update WIPRO_LOCAL_KEY in this script.\n"
        "   (Also confirm the IP is still correct: python -m tinytuya scan)"
    )
    sys.exit(1)

wipro_bulb.turn_on()

# try:
#     halonix_bulb = tinytuya.BulbDevice(HALONIX_DEV_ID, HALONIX_IP, HALONIX_LOCAL_KEY)
#     halonix_bulb.set_version(3.3)
#     halonix_bulb.turn_on()
#     print("✅ Halonix Bulb Connected")
# except Exception as e:
#     print(f"❌ Failed to connect to Halonix Bulb: {e}")

# ==========================================
# 3. SCREEN CAPTURE & COLOR PROCESSING
# ==========================================
def get_screen_color(sct):
    """
    Captures the primary monitor and returns a saturation-weighted average
    color. Weighting by saturation lets vivid content (explosions, neon,
    grass) dominate over grey/white UI chrome and letterbox bars, which is
    much closer to how Philips Ambilight picks its color.
    """
    try:
        # Monitor 1 is usually the primary display in mss
        monitor = sct.monitors[1]
        sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        # Downscale drastically — 64x36 is plenty for a single bulb
        img = img.resize((64, 36), Image.Resampling.NEAREST)
        px = np.asarray(img, dtype=np.float32)

        # Per-pixel saturation proxy: max(R,G,B) - min(R,G,B)
        sat = px.max(axis=2) - px.min(axis=2)
        weights = sat + 1.0  # +1 so pure-grey frames still average sanely

        wsum = weights.sum()
        r = float((px[..., 0] * weights).sum() / wsum)
        g = float((px[..., 1] * weights).sum() / wsum)
        b = float((px[..., 2] * weights).sum() / wsum)
        return r, g, b

    except Exception as e:
        print(f"Error capturing screen: {e}")
        return 0.0, 0.0, 0.0


def enhance_color(r, g, b):
    """
    Boost saturation and lift dark scenes so the bulb output feels like
    ambilight instead of a dim grey glow.
    """
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    s = min(1.0, s * SATURATION_BOOST)
    v = max(MIN_BRIGHTNESS, v ** GAMMA)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return r * 255.0, g * 255.0, b * 255.0

# ==========================================
# 4. MAIN SYNC LOOP
# ==========================================
print("Starting Ambilight Sync... Press Ctrl+C to stop.")

# Switch to colour mode once, not every frame
try:
    wipro_bulb.set_mode('colour')
except Exception as e:
    print(f"⚠️  Could not set colour mode: {e}")

with mss.MSS() as sct:
    # Start the smoothed color at whatever is on screen right now
    current = np.array(enhance_color(*get_screen_color(sct)))
    last_sent = np.array([-255.0, -255.0, -255.0])  # force first send

    try:
        while True:
            # 1. Capture and enhance the target color
            target = np.array(enhance_color(*get_screen_color(sct)))

            # 2. Ease toward it (exponential moving average = smooth fades)
            current += (target - current) * SMOOTHING
            r, g, b = (int(c) for c in current.clip(0, 255))

            print(f"Color -> R: {r:3} | G: {g:3} | B: {b:3}", end="\r")

            # 3. Only talk to the bulb if the color moved enough to notice
            if np.abs(current - last_sent).max() >= COLOR_DELTA:
                try:
                    wipro_bulb.set_colour(r, g, b, nowait=True)
                    last_sent = current.copy()
                except Exception as e:
                    print(f"\n❌ Error sending to Wipro: {e}")

            time.sleep(UPDATE_DELAY)

    except KeyboardInterrupt:
        print("\nAmbilight Sync Stopped by User.")
        try:
            wipro_bulb.set_white()  # hand the bulb back in a usable state
        except Exception:
            pass
