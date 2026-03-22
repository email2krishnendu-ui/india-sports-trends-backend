"""
Run this ONCE locally to generate your VAPID keys for Web Push.

    pip install py-vapid
    python gen_vapid_keys.py

Copy the output into your Render environment variables:
  VAPID_PUBLIC_KEY   → paste into Render backend env + frontend index.html
  VAPID_PRIVATE_KEY  → paste into Render backend env only (keep secret)
  VAPID_CLAIMS_EMAIL → your email address
"""

from py_vapid import Vapid
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption
)
import base64

v = Vapid()
v.generate_keys()

pub_bytes = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
pub_b64   = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

priv_pem  = v.private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
priv_b64  = base64.urlsafe_b64encode(priv_pem).rstrip(b"=").decode()

print("\n" + "="*60)
print("  VAPID Keys — copy these into Render environment variables")
print("="*60)
print(f"\nVAPID_PUBLIC_KEY={pub_b64}")
print(f"\nVAPID_PRIVATE_KEY={priv_b64}")
print("\nVAPID_CLAIMS_EMAIL=mailto:you@example.com")
print("\n" + "="*60)
print("Also paste VAPID_PUBLIC_KEY into frontend/index.html")
print("Search for: VAPID_PUBLIC_KEY_HERE")
print("="*60 + "\n")
