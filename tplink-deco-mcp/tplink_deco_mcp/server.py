"""
MCP server for TP-Link Deco mesh routers.

Uses the unofficial Deco admin API (reverse-engineered from the Deco app).
Authentication uses a custom RSA+AES encryption scheme:
  1. Fetch RSA public keys from the router
  2. Generate a random AES-128 key+IV per session
  3. Encrypt the password with RSA, sign the AES params with RSA
  4. All subsequent request payloads are AES-encrypted; responses are AES-decrypted

Environment variables:
  DECO_HOST      Router IP or hostname (default: 192.168.1.1)
  DECO_USERNAME  Admin username (default: admin)
  DECO_PASSWORD  Admin password (required)
"""

import base64
import hashlib
import json
import math
import os
import re
import secrets
from urllib.parse import quote_plus

import httpx
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from cryptography.hazmat.primitives import padding as crypto_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tplink-deco-mcp")

_HOST = os.environ.get("DECO_HOST", "192.168.1.1")
_USERNAME = os.environ.get("DECO_USERNAME", "admin")
_PASSWORD = os.environ.get("DECO_PASSWORD", "")

_AES_KEY_BYTES = 16
_MIN_AES = 10 ** (_AES_KEY_BYTES - 1)
_MAX_AES = (10 ** _AES_KEY_BYTES) - 1
_PKCS1_HEADER = 11


# ── Crypto helpers ─────────────────────────────────────────────────────────────

def _byte_len(n: int) -> int:
    return (int(math.log2(n)) + 8) >> 3


def _rsa_encrypt(n: int, e: int, plaintext: bytes) -> str:
    """RSA-PKCS1v1.5 encrypt, split into blocks, return hex string."""
    key = RSA.construct((n, e)).publickey()
    enc = PKCS1_v1_5.new(key)
    block = _byte_len(n) - _PKCS1_HEADER
    result, i = "", 0
    while i < len(plaintext):
        result += enc.encrypt(plaintext[i:i + block]).hex()
        i += block
    return result


def _aes_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    padder = crypto_padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _aes_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    return dec.update(ciphertext) + dec.finalize()


def _b64_decode_name(name: str) -> str:
    try:
        return base64.b64decode(name).decode()
    except Exception:
        return name


# ── Deco API client ────────────────────────────────────────────────────────────

class _DecoClient:
    def __init__(self, host: str, username: str, password: str):
        self._host = f"http://{host}" if not host.startswith("http") else host.rstrip("/")
        self._username = username
        self._password = password
        self._reset()

    def _reset(self):
        self._stok = None
        self._cookie = None
        self._seq = None
        self._aes_key = self._aes_iv = None
        self._aes_key_b = self._aes_iv_b = None
        self._pw_rsa_n = self._pw_rsa_e = None
        self._sign_rsa_n = self._sign_rsa_e = None

    def _gen_aes(self):
        self._aes_key = secrets.randbelow(_MAX_AES - _MIN_AES) + _MIN_AES
        self._aes_iv  = secrets.randbelow(_MAX_AES - _MIN_AES) + _MIN_AES
        self._aes_key_b = str(self._aes_key).encode()
        self._aes_iv_b  = str(self._aes_iv).encode()

    def _post(self, url: str, params: dict, body: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self._host}/webpages/index.html",
        }
        cookies = {}
        if self._cookie:
            k, _, v = self._cookie.partition("=")
            cookies[k] = v
        r = httpx.post(url, params=params, content=body.encode(),
                       headers=headers, cookies=cookies, timeout=10, verify=False)
        r.raise_for_status()
        for name, value in r.headers.multi_items():
            if name.lower() == "set-cookie":
                m = re.search(r"sysauth=[a-f0-9]+", value)
                if m:
                    self._cookie = m.group(0)
        return r.json()

    def _fetch_keys(self):
        url = f"{self._host}/cgi-bin/luci/;stok=/login"
        resp = self._post(url, {"form": "keys"}, json.dumps({"operation": "read"}))
        keys = resp["result"]["password"]
        self._pw_rsa_n = int(keys[0], 16)
        self._pw_rsa_e = int(keys[1], 16)

    def _fetch_auth(self):
        url = f"{self._host}/cgi-bin/luci/;stok=/login"
        resp = self._post(url, {"form": "auth"}, json.dumps({"operation": "read"}))
        auth = resp["result"]
        self._sign_rsa_n = int(auth["key"][0], 16)
        self._sign_rsa_e = int(auth["key"][1], 16)
        self._seq = auth["seq"]

    def _encode_data(self, payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.b64encode(_aes_encrypt(self._aes_key_b, self._aes_iv_b, raw)).decode()

    def _encode_sign(self, data_len: int) -> str:
        h = hashlib.md5(f"{self._username}{self._password}".encode()).hexdigest()
        text = f"k={self._aes_key}&i={self._aes_iv}&h={h}&s={self._seq + data_len}"
        return _rsa_encrypt(self._sign_rsa_n, self._sign_rsa_e, text.encode())

    def _encode_payload(self, payload: dict) -> str:
        data = self._encode_data(payload)
        return f"sign={self._encode_sign(len(data))}&data={quote_plus(data)}"

    def _decrypt(self, data: str) -> dict:
        raw = _aes_decrypt(self._aes_key_b, self._aes_iv_b, base64.b64decode(data))
        pad = raw[-1]
        return json.loads(raw[:-pad].decode())

    def login(self):
        self._reset()
        self._gen_aes()
        self._fetch_keys()
        self._fetch_auth()
        pw_enc = _rsa_encrypt(self._pw_rsa_n, self._pw_rsa_e, self._password.encode())
        payload = {"params": {"password": pw_enc}, "operation": "login"}
        url = f"{self._host}/cgi-bin/luci/;stok=/login"
        resp = self._post(url, {"form": "login"}, self._encode_payload(payload))
        data = self._decrypt(resp["data"])
        if data.get("error_code") != 0:
            raise RuntimeError(f"Login failed (error_code={data.get('error_code')})")
        self._stok = data["result"]["stok"]

    def _ensure_auth(self):
        if not self._stok:
            self.login()

    def _admin(self, endpoint: str, form: str, payload: dict) -> dict:
        self._ensure_auth()
        url = f"{self._host}/cgi-bin/luci/;stok={self._stok}/admin/{endpoint}"
        try:
            resp = self._post(url, {"form": form}, self._encode_payload(payload))
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                self.login()
                url = f"{self._host}/cgi-bin/luci/;stok={self._stok}/admin/{endpoint}"
                resp = self._post(url, {"form": form}, self._encode_payload(payload))
            else:
                raise
        return self._decrypt(resp["data"])

    def list_clients(self, deco_mac: str = "default") -> list[dict]:
        data = self._admin("client", "client_list", {
            "operation": "read",
            "params": {"device_mac": deco_mac},
        })
        clients = data["result"]["client_list"]
        for c in clients:
            if "name" in c:
                c["name"] = _b64_decode_name(c["name"])
        return clients

    def list_devices(self) -> list[dict]:
        data = self._admin("device", "device_list", {"operation": "read"})
        devices = data["result"]["device_list"]
        for d in devices:
            for field in ("custom_nickname", "nickname"):
                if field in d:
                    d[field] = _b64_decode_name(d[field])
        return devices

    def reboot(self, mac_addresses: list[str]):
        data = self._admin("device", "system", {
            "operation": "reboot",
            "params": {"mac_list": [{"mac": m} for m in mac_addresses]},
        })
        if data.get("error_code") != 0:
            raise RuntimeError(f"Reboot failed: {data}")

    def logout(self):
        if not self._stok:
            return
        url = f"{self._host}/cgi-bin/luci/;stok={self._stok}/admin/system"
        try:
            self._post(url, {"form": "logout"}, self._encode_payload({"operation": "write"}))
        finally:
            self._reset()


_client = _DecoClient(_HOST, _USERNAME, _PASSWORD)


# ── MCP tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
def list_clients() -> list[dict]:
    """List all devices currently connected to the Deco mesh network.

    Returns name, MAC address, IP address, connection type (2.4GHz/5GHz/wired),
    and which Deco node they are connected to.
    """
    return _client.list_clients()


@mcp.tool()
def list_deco_nodes() -> list[dict]:
    """List all Deco mesh nodes and their status.

    Returns each node's name, MAC address, IP address, firmware version,
    hardware version, and connection status.
    """
    return _client.list_devices()


@mcp.tool()
def reboot_deco_nodes(mac_addresses: list[str]) -> str:
    """Reboot one or more Deco nodes.

    Args:
        mac_addresses: List of node MAC addresses to reboot (e.g. ["AA-BB-CC-DD-EE-FF"]).
                       Use list_deco_nodes() to find MAC addresses.
                       To reboot all nodes, pass all MACs from list_deco_nodes().
    """
    _client.reboot(mac_addresses)
    return f"Reboot initiated for {len(mac_addresses)} node(s)"


@mcp.tool()
def logout_deco() -> str:
    """Log out of the Deco admin session. The next tool call will re-authenticate automatically."""
    _client.logout()
    return "Logged out"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
