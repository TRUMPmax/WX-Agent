from __future__ import annotations

import base64
import hashlib
import os
import struct
import time
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _sha1_hex(parts: list[str]) -> str:
    items = [p for p in parts if p is not None]
    items.sort()
    raw = "".join(items)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def verify_signature(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    return _sha1_hex([token, timestamp, nonce]) == signature


def verify_msg_signature(token: str, msg_signature: str, timestamp: str, nonce: str, encrypted: str) -> bool:
    return _sha1_hex([token, timestamp, nonce, encrypted]) == msg_signature


def parse_wechat_xml(xml_body: str) -> dict[str, str]:
    root = ET.fromstring(xml_body)
    result: dict[str, str] = {}
    for child in root:
        result[child.tag] = child.text or ""
    return result


def build_text_reply(to_user: str, from_user: str, content: str) -> str:
    now = int(time.time())
    escaped_content = (content or "").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{now}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{escaped_content}]]></Content>"
        "</xml>"
    )


def decrypt_wechat_message(encrypted_b64: str, encoding_aes_key: str, app_id: str) -> str:
    if not encoding_aes_key:
        raise ValueError("WECHAT_ENCODING_AES_KEY is required for encrypted mode")
    aes_key = base64.b64decode(encoding_aes_key + "=")
    iv = aes_key[:16]
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    encrypted = base64.b64decode(encrypted_b64)
    padded = decryptor.update(encrypted) + decryptor.finalize()
    plain = _pkcs7_unpad(padded)

    content = plain[16:]
    xml_len = struct.unpack("!I", content[:4])[0]
    xml_bytes = content[4 : 4 + xml_len]
    from_app_id = content[4 + xml_len :].decode("utf-8")
    if app_id and from_app_id != app_id:
        raise ValueError("AppID mismatch while decrypting WeChat message")
    return xml_bytes.decode("utf-8")


def encrypt_wechat_message(plain_xml: str, encoding_aes_key: str, app_id: str) -> str:
    if not encoding_aes_key:
        raise ValueError("WECHAT_ENCODING_AES_KEY is required for encrypted mode")
    aes_key = base64.b64decode(encoding_aes_key + "=")
    iv = aes_key[:16]

    msg = plain_xml.encode("utf-8")
    app_bytes = app_id.encode("utf-8")
    random16 = os.urandom(16)
    msg_len = struct.pack("!I", len(msg))
    raw = random16 + msg_len + msg + app_bytes
    padded = _pkcs7_pad(raw)

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("utf-8")


def build_encrypted_reply(
    plain_xml: str,
    token: str,
    encoding_aes_key: str,
    app_id: str,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> str:
    ts = timestamp or str(int(time.time()))
    nn = nonce or hashlib.md5(os.urandom(16)).hexdigest()[:10]
    encrypted = encrypt_wechat_message(plain_xml, encoding_aes_key, app_id)
    signature = _sha1_hex([token, ts, nn, encrypted])
    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{ts}</TimeStamp>"
        f"<Nonce><![CDATA[{nn}]]></Nonce>"
        "</xml>"
    )


def _pkcs7_pad(data: bytes, block_size: int = 32) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    if pad_len == 0:
        pad_len = block_size
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes, block_size: int = 32) -> bytes:
    if not data:
        raise ValueError("Invalid PKCS7 data: empty")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        raise ValueError("Invalid PKCS7 padding")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("Invalid PKCS7 padding bytes")
    return data[:-pad_len]
