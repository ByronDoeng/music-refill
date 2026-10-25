"""网易云盘文件上传（移植自 api-enhanced module/cloud.js + plugins/songUpload.js）。

流程：upload/check（秒传判定）→ nos/token/alloc → LBS → NOS 直传 → info/v2 → pub/v2。
check 返回 needUpload=false 时文件 MD5 已在网易服务器（秒传），跳过 NOS 直传。
对本项目：缺口歌都是不可播的，拿不到库内文件 MD5，秒传基本不触发，属兜底逻辑。
所有 music.163.com 调用带重试（大文件上传后服务端处理慢，偶发读超时）。
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import requests

BUCKET = "jd-musicrep-privatecloud-audio-public"
MIME = {"mp3": "audio/mpeg", "flac": "audio/flac"}
CHUNK = 1 << 20  # 1MB


class UploadError(Exception):
    pass


def _call(client, path: str, payload: dict, attempts: int = 3, timeout: int = 60) -> dict:
    """weapi 调用 + 网络类异常重试（幂等接口：check/token/info/pub 均可安全重发）。"""
    last = None
    for i in range(attempts):
        try:
            return client._weapi(path, payload, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last = e
            time.sleep(3 + i * 3)
    raise UploadError(f"{path} 连续 {attempts} 次网络失败: {str(last)[:120]}")


def _sanitize_stem(stem: str) -> str:
    # token/alloc 的 filename 规范：去扩展名、去空白、点换下划线（api-enhanced sanitizeFilename）
    return stem.replace(" ", "").replace(".", "_") or "unknown"


def _upload_chunks(data: bytes, progress_cb=None):
    """已废弃：NOS 不接受 generator 流式体（连接被重置 10054）。

    保留函数体仅为注释记忆；上传必须用整块 bytes（data=data）。
    """
    raise NotImplementedError("NOS 直传必须整块发送，见 upload_cloud_file")


def upload_cloud_file(
    client,
    file_path: Path,
    song: str,
    artist: str,
    album: str,
    bitrate: int = 999000,
    upload_progress_cb=None,
) -> dict:
    """上传单个文件到网易云盘。返回 {songId, matched_library, need_upload}。

    upload_progress_cb(sent, total)：NOS 直传阶段的字节进度回调（秒传时不会触发）。
    """
    data = file_path.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    size = len(data)
    ext = (file_path.suffix.lstrip(".") or "mp3").lower()
    filename = file_path.name

    # 1) check：秒传判定 + 返回已有 songId
    check = _call(
        client, "/api/cloud/upload/check",
        {"bitrate": str(bitrate), "ext": "", "length": size, "md5": md5, "songId": "0", "version": 1},
    )
    song_id = str(check.get("songId") or "0")
    need_upload = bool(check.get("needUpload", True))

    # 2) token alloc
    token_res = _call(
        client, "/api/nos/token/alloc",
        {
            "bucket": "",
            "ext": ext,
            "filename": _sanitize_stem(file_path.stem),
            "local": False,
            "nos_product": 3,
            "type": "audio",
            "md5": md5,
        },
    )
    result = token_res.get("result") or {}
    object_key, token, resource_id = result.get("objectKey"), result.get("token"), result.get("resourceId")
    if not resource_id or not object_key or not token:
        raise UploadError(f"token/alloc 失败: {str(token_res)[:150]}")

    # 3) NOS 直传（秒传时跳过）
    if need_upload:
        lbs = requests.get(
            "https://wanproxy.127.net/lbs",
            params={"version": "1.0", "bucketname": BUCKET},
            timeout=15,
        ).json()
        host = (lbs.get("upload") or [None])[0]
        if not host:
            raise UploadError(f"LBS 无可用上传节点: {str(lbs)[:100]}")
        if not host.startswith("http"):  # LBS 返回的节点可能自带 scheme
            host = "https://" + host
        url = f"{host}/{BUCKET}/{object_key.replace('/', '%2F')}?offset=0&complete=true&version=1.0"
        r = requests.post(
            url,
            headers={
                "x-nos-token": token,
                "Content-MD5": md5,  # 网易 NOS 接受 hex 格式（同 api-enhanced）
                "Content-Type": MIME.get(ext, "audio/mpeg"),
                "Content-Length": str(size),
            },
            data=data,  # 必须整块 bytes：generator/流式体会被 NOS 重置连接（实测 10054）
            timeout=600,
        )
        if r.status_code != 200:
            raise UploadError(f"NOS 上传失败 HTTP {r.status_code}: {r.text[:100]}")

    # 4) info/v2：登记云盘信息（song/artist/album 用曲库官方元数据，决定匹配率）。
    # 大文件上传后服务端索引需要时间，偶发非 200（uploadStatus 6=校验未通过/未就绪），
    # 重试轮询几次；持续失败才报错（此时需完整重走上传流程）。
    info = None
    payload = {
        "md5": md5,
        "songid": song_id,
        "filename": filename,
        "song": song or filename,
        "album": album or "未知专辑",
        "artist": artist or "未知艺术家",
        "bitrate": str(bitrate),
        "resourceId": resource_id,
    }
    for attempt in range(4):
        info = _call(client, "/api/upload/cloud/info/v2", payload)
        if info.get("code") == 200:
            break
        time.sleep(4 + attempt * 3)
    if not info or info.get("code") != 200:
        raise UploadError(f"info/v2 失败（已重试 4 次）: {str(info)[:150]}")
    final_id = info.get("songId") or song_id

    # 5) pub/v2：发布（触发匹配）
    _call(client, "/api/cloud/pub/v2", {"songid": final_id})

    return {
        "songId": final_id,
        # check.songId 为纯数字 = 秒传命中已有曲库条目；长哈希 = 私有资源 id（匹配与否以客户端云盘页为准）
        "matched_library": song_id.isdigit() and song_id != "0",
        "need_upload": need_upload,
        "private_resource_id": check.get("songId"),
    }
