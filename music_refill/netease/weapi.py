"""网易云音乐 WEAPI 加密。

weapi 是网页端通用的请求加密格式：
    payload(JSON) -> AES-CBC(固定key) -> AES-CBC(随机key) -> params
    随机key 反转后 RSA(无填充) -> encSecKey
算法与社区 NeteaseCloudMusicApi 实现一致，key 为公开常量；本项目只用它
访问"自己账号有权访问"的数据（登录态接口），不涉及绕过任何权限。
"""
from __future__ import annotations

import base64
import json
import random
import string

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

PRESET_KEY = b"0CoJUm6Qyw8W8jud"
IV = b"0102030405060708"
RSA_E = 0x010001
# 网易公开 RSA modulus（社区项目通用常量）
RSA_N = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf69528"
    "0104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee2559"
    "32575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc69"
    "35b3ece0462db0a22b8e7",
    16,
)
BASE62 = string.ascii_letters + string.digits


def _aes_cbc_b64(data: bytes, key: bytes) -> str:
    cipher = AES.new(key, AES.MODE_CBC, IV)
    return base64.b64encode(cipher.encrypt(pad(data, AES.block_size))).decode()


def _rsa_no_pad(text: str) -> str:
    """网易的自定义 RSA：明文反转后直接做模幂，输出定长 hex（无标准填充）。"""
    secret = int(text[::-1].encode().hex(), 16)
    return f"{pow(secret, RSA_E, RSA_N):0256x}"


def encrypt(payload: dict) -> dict[str, str]:
    text = json.dumps(payload, separators=(",", ":")).encode()
    secret = "".join(random.choices(BASE62, k=16))
    params = _aes_cbc_b64(_aes_cbc_b64(text, PRESET_KEY).encode(), secret.encode())
    return {"params": params, "encSecKey": _rsa_no_pad(secret)}
