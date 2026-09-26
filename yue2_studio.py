"""乐坊 YuE2 Studio服务端（纯 Python 标准库，单文件）

一个面板 = 一个 HTTP 服务 + 若干作业线程。跟 ComfyUI 之间**只有两条缝**：
`studio.json` 里的 comfy_root（读它的 output\）和 127.0.0.1:8188 的 HTTP/WebSocket。
不 import ComfyUI、不用它的 venv、也不假设它在隔壁目录。

    python yue2_studio.py                  # 起面板（默认 8190）
    python yue2_studio.py --port 8190
    python yue2_studio.py --print-config   # 打印解析后的配置（排查用）

零第三方依赖：只用标准库。前端也是单文件 + 本地 vendor\，不连 CDN。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 起子进程（ComfyUI / nvidia-smi / curl）一律不要黑窗。
# CREATE_NO_WINDOW 而不是 DETACHED_PROCESS：venv 的 Scripts\python.exe 只是个 redirector，
# 它还会再 CreateProcess 一次去起真解释器；DETACHED 把 console 整个拿掉 → 孩子自己新分配一个
# → 每次冷启在屏幕上弹一个黑窗。给它一个"没有窗口的 console"就够。
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_NEW_GROUP = 0x00000200 if os.name == "nt" else 0

# ---------- 面板自己的文件 ----------
ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "yue2_studio.html"
VENDOR = ROOT / "vendor"          # 本地内置的前端库（gsap/Flip），由 /vendor/<name>.js 提供

PANEL_PORT = int(os.environ.get("YUE2_PANEL_PORT", "8190"))
COMFY_PORT = int(os.environ.get("YUE2_COMFY_PORT", "8188"))


# ---------- 面板 ↔ ComfyUI 的接缝（全文件只有这一段认 ComfyUI 的位置）----------
def _read_config() -> dict:
    """本目录下的 studio.json（可选）。放的是 ComfyUI 在哪、用哪个 python 起它。"""
    p = ROOT / "studio.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"studio.json 读不动，按空配置继续: {e}", file=sys.stderr)
        return {}


CFG = _read_config()


def _cfg_path(key: str, default, env: str | None = None) -> Path:
    """环境变量 YUE2_<KEY> > studio.json 的 <key> > 默认值。相对路径按**面板目录**解析。"""
    v = os.environ.get(env or ("YUE2_" + key.upper())) or CFG.get(key)
    if not v:
        return Path(default).resolve()
    p = Path(str(v)).expanduser()
    return (p if p.is_absolute() else ROOT / p).resolve()


COMFY = _cfg_path("comfy_root", ROOT / "ComfyUI")
OUT = _cfg_path("output_dir", COMFY / "output")
SONGS = _cfg_path("songs_dir", OUT / "music", env="YUE2_SONGS_DIR")
PY = _cfg_path("comfy_python", COMFY / ".venv" / "Scripts" / "python.exe")
LOG = _cfg_path("comfy_log", COMFY / "comfy_server.log")
LOG_ERR = LOG.parent / (LOG.name + ".err")
# 拉起 ComfyUI 的命令行（工作目录 = COMFY）
COMFY_CMD = [str(x) for x in (CFG.get("comfy_cmd")
                              or ["main.py", "--listen", "127.0.0.1", "--port", str(COMFY_PORT)])]
# 面板要不要自己把 ComfyUI 拉起来（false = 只当前端，服务由外部管）
COMFY_AUTOSTART = str(os.environ.get("YUE2_COMFY_AUTOSTART", CFG.get("comfy_autostart", True))).lower() \
    not in ("0", "false", "no", "")

# ---------- YuE2 权重（VAE + 文本编码器都在这一个文件里）----------
CKPT_NAME = "yue2_3b_int8_convrot.safetensors"
CKPT_BYTES = 3960938800
CKPT_SHA256 = "96fe199377309001ed8cd26a944baeee8cc31a20ba7c36d1d3c0a7e1f4149db6"
CKPT_URL = "https://hf-mirror.com/Comfy-Org/YuE2/resolve/main/checkpoints/yue2_3b_int8_convrot.safetensors"
CKPT = COMFY / "models" / "checkpoints" / CKPT_NAME

# 创作页的**四组**预设（曲风 / 人声 / 乐器 / BPM 分开点，中文），可在 studio.json 的 presets 里改。
# 组名就是侧栏里那一行的标签；组里每一项是**最终风格句的一个片段**（BPM 那组带 " BPM" 后缀）。
DEFAULT_PRESETS = {
    "曲风": ["民谣", "流行", "城市流行", "爵士", "摇滚", "电子", "古风", "环境"],
    "人声": ["女声", "男声", "双人合唱", "童声", "无人声（纯音乐）"],
    "乐器": ["钢琴", "木吉他", "弦乐", "合成器", "鼓组", "中国民乐"],
    "BPM": ["68 BPM", "76 BPM", "88 BPM", "96 BPM", "108 BPM", "120 BPM"],
}
_raw_presets = CFG.get("presets")
PRESETS = ({str(k): [str(x) for x in v] for k, v in _raw_presets.items() if isinstance(v, list) and v}
           if isinstance(_raw_presets, dict) and _raw_presets else dict(DEFAULT_PRESETS))

# 服务之间的调用不走代理：系统里开着代理（尤其把 localhost 也塞进去的那种）会让本机请求全超时。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------- 自动调参（可选）：一句话交给配置的大模型，换回歌词 + 各项参数 ----------
# 面板自己不装模型：只当**任何 OpenAI 兼容端点**（`POST <base_url>/chat/completions`）的客户端。
# 默认什么都不填（不挑厂商）；**没配就禁用「自动生成」**，出歌链路一点不受影响。
LLM_CFG = CFG.get("llm") if isinstance(CFG.get("llm"), dict) else {}


def _llm_opt(key: str, env: str, default):
    v = os.environ.get(env) or LLM_CFG.get(key)
    return default if v in (None, "") else v


LLM_BASE = str(_llm_opt("base_url", "YUE2_LLM_BASE_URL", "")).rstrip("/")
LLM_MODEL = str(_llm_opt("model", "YUE2_LLM_MODEL", ""))
LLM_PROXY = str(_llm_opt("proxy", "YUE2_LLM_PROXY", ""))
LLM_TIMEOUT = float(_llm_opt("timeout", "YUE2_LLM_TIMEOUT", 90))
LLM_KEY_ENV = str(_llm_opt("api_key_env", "YUE2_LLM_API_KEY_ENV", "YUE2_LLM_API_KEY"))
# 思考型模型会先烧 token 想，给少了正文会被截掉 —— 默认给足
LLM_MAX_TOKENS = int(_llm_opt("max_tokens", "YUE2_LLM_MAX_TOKENS", 4000))
# key 只从环境变量 / Windows 注册表读：别写进 studio.json
_LLM_OPENER = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": LLM_PROXY, "https": LLM_PROXY}))
               if LLM_PROXY else urllib.request.build_opener(urllib.request.ProxyHandler({})))


def _win_env(name: str) -> str:
    """Windows：进程环境里没有就去**注册表**里找（`setx` 写的正是 `HKCU\\Environment`）。
    从"比 setx 更早"的 shell / 服务里起的面板就是这样把 key 找回来的 ——
    否则会出现"明明配过了，面板还是把自动生成禁用"这种（用户实测踩过）。"""
    if os.name != "nt" or not name:
        return ""
    try:
        import winreg
    except ImportError:  # noqa: BLE001
        return ""
    for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, sub) as k:
                v, _t = winreg.QueryValueEx(k, name)
                if v:
                    return str(v)
        except OSError:
            continue
    return ""


def _llm_key() -> str:
    for name in ("YUE2_LLM_API_KEY", LLM_KEY_ENV):
        v = os.environ.get(name) or _win_env(name)
        if v:
            return str(v)
    return ""


def llm_ready() -> bool:
    """三样齐了才算配好：端点 + 模型名 + key。缺哪样，「自动生成」那格就禁用并写明原因。"""
    return bool(LLM_BASE and LLM_MODEL and _llm_key())


def llm_why() -> str:
    """没配好时给前端一句人话（写进那一格的副标题）。"""
    if not LLM_BASE or not LLM_MODEL:
        return "没配大模型：studio.json 的 llm.base_url / llm.model（任何 OpenAI 兼容端点）"
    if not _llm_key():
        return f"没读到 key：设环境变量 {LLM_KEY_ENV}（或 YUE2_LLM_API_KEY）后重启面板"
    return ""



def _api(path: str, payload: dict | None = None, timeout: float = 30.0, method: str | None = None):
    """打 ComfyUI 的 HTTP。payload=None 就是 GET，给了就是 POST（显式指定 method 时可空体 POST）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{COMFY_PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method=method or ("POST" if data is not None else "GET"))
    with _OPENER.open(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


_SYS_CACHE = {"t": 0.0, "online": False, "version": None}


def _comfy_stats(ttl: float = 2.0) -> tuple[bool, str | None]:
    """ComfyUI 在线吗 + 版本号。/api/state 会被前端一秒一读，别每次都真去打一遍。"""
    now = time.time()
    if ttl > 0 and now - _SYS_CACHE["t"] < ttl:
        return bool(_SYS_CACHE["online"]), _SYS_CACHE["version"]
    try:
        d = _api("/system_stats", timeout=4)
        ver = (d.get("system") or {}).get("comfyui_version")
        online, version = True, (str(ver) if ver else None)
    except Exception:
        online, version = False, None
    _SYS_CACHE.update(t=now, online=online, version=version)
    return online, version


def comfy_online() -> bool:
    return _comfy_stats(ttl=0)[0]


def _port_taken(port: int) -> bool:
    """端口上有没有人在听（不关心是不是 ComfyUI）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def start_comfy() -> tuple[bool, str, float]:
    """把 ComfyUI 拉起来，等到 /system_stats 就绪。返回（成不成, 失败原因, 秒数）。

    已经在跑就直接返回；端口被别的程序占着就明确报错 —— **绝不误杀**别人的进程。
    """
    t0 = time.time()
    if comfy_online():
        return True, "", 0.0
    if _port_taken(COMFY_PORT):
        return False, f"端口 {COMFY_PORT} 被别的程序占用了（连得上但不是 ComfyUI），面板不会去动它", time.time() - t0
    if not COMFY_AUTOSTART:
        return False, "配置里关了自动拉起（comfy_autostart=false），请自己起 ComfyUI", time.time() - t0
    if not COMFY.is_dir():
        return False, f"找不到 ComfyUI 目录 {COMFY}（在 studio.json 里改 comfy_root）", time.time() - t0
    if not PY.exists():
        return False, f"找不到 ComfyUI 的 python: {PY}（在 studio.json 里改 comfy_python）", time.time() - t0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    flags = _NO_WINDOW | _NEW_GROUP
    try:
        with open(LOG, "ab") as fh, open(LOG_ERR, "ab") as eh:
            subprocess.Popen([str(PY), *COMFY_CMD], cwd=str(COMFY), stdin=subprocess.DEVNULL,
                             stdout=fh, stderr=eh, creationflags=flags, close_fds=True)
    except OSError as e:
        return False, f"起 ComfyUI 失败: {e}", time.time() - t0
    while time.time() - t0 < 300:
        if comfy_online():
            return True, "", time.time() - t0
        time.sleep(2)
    return False, f"起来了但 {COMFY_PORT} 三百秒还没就绪，看日志 {LOG}", time.time() - t0


# ---------- 图（与 YuE2 的官方文生乐模板等价，节点 id 照抄）----------

def _abc_inputs(style: str, lyrics: str, seed: int, mode: str) -> dict:
    return {"clip": ["15", 1], "style": style, "lyrics": lyrics, "seed": int(seed), "mode": mode,
            "max_abc_tokens": 8192, "temperature": 0.7, "top_p": 0.9, "top_k": 30,
            "repetition_penalty": 1.005, "penalty_window": 100}


def graph_song(style: str, lyrics: str, seconds: float, seed: int, steps: int, mode: str) -> dict:
    """出歌图。mode=off 的语义 = 不提交 ABC 节点、abc 给空串（节点自己吃这个语义），
    YuE2GenerateMusic 的 mode 走 full（YuE2 的 mode 只有 full/melody，没有 off）。"""
    g: dict = {"15": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT_NAME}}}
    if mode == "off":
        abc_ref: object = ""
        music_mode = "full"
    else:
        g["24"] = {"class_type": "YuE2GenerateABC", "inputs": _abc_inputs(style, lyrics, seed, mode)}
        # 把同一次生成的 ABC 也挂出来：出完歌能连谱一起落盘（有谱才有底部那个切换滑块）
        g["14"] = {"class_type": "PreviewAny", "inputs": {"source": ["24", 0]}}
        abc_ref = ["24", 0]
        music_mode = mode
    g["18"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["25", 0]}}
    g["25"] = {"class_type": "YuE2GenerateMusic",
               "inputs": {"clip": ["15", 1], "style": style, "lyrics": lyrics, "abc": abc_ref,
                          "seed": int(seed), "mode": music_mode, "max_duration": round(float(seconds), 2),
                          "temperature": 1.0, "top_p": 0.95, "top_k": 100, "repetition_penalty": 1.2}}
    # seconds 必须接 YuE2GenerateMusic 的第二个输出（模型自己算的时长），不要手填
    g["5"] = {"class_type": "EmptyYuE2LatentAudio", "inputs": {"seconds": ["25", 1], "batch_size": 1}}
    g["8"] = {"class_type": "KSampler",
              "inputs": {"model": ["15", 0], "positive": ["25", 0], "negative": ["18", 0],
                         "latent_image": ["5", 0], "seed": 42, "steps": int(steps), "cfg": 1.0,
                         "sampler_name": "dpm_2", "scheduler": "sgm_uniform", "denoise": 1.0}}
    g["9"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["15", 2]}}
    g["10"] = {"class_type": "SaveAudioAdvanced",
               "inputs": {"audio": ["9", 0], "filename_prefix": "audio/music_panel", "format": "flac"}}
    return g


def graph_plan(style: str, lyrics: str, seed: int, mode: str) -> dict:
    """只出乐谱：ABC 文本挂一个 PreviewAny，文本从 /history/<id> 的 outputs.<id>.text 拿。"""
    m = "full" if mode == "off" else mode      # 出谱本身就是要乐谱，off 没意义 → 按 full
    return {
        "15": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT_NAME}},
        "24": {"class_type": "YuE2GenerateABC", "inputs": _abc_inputs(style, lyrics, seed, m)},
        "14": {"class_type": "PreviewAny", "inputs": {"source": ["24", 0]}},
    }


# ---------- 最小 WebSocket 客户端（只为拿真实采样步数）----------
# ComfyUI 每采样一步就把 {"type":"progress","data":{value,max}} 发到 /ws，
# 但**只发给提交这个 prompt 的那个 client_id**，所以这里必须和 /prompt 用同一个 id。
# 这条链路是"锦上添花"：连不上就让 job["steps"] 保持 null（前端只显示秒数），绝不能拖垮任务。
_PHASE_BY_NODE = {"15": "准备中", "24": "乐谱规划中", "25": "采样中", "8": "采样中", "9": "解码中"}
_WS_MAX_FRAME = 64 * 1024 * 1024        # 预览图帧也不会这么大；超了说明流乱了


def _ws_send(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    """客户端 → 服务端的帧**必须**加掩码（RFC 6455）。"""
    key = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    sock.sendall(head + key + bytes(c ^ key[i % 4] for i, c in enumerate(payload)))


def _ws_take_frame(buf: bytearray):
    """缓冲里凑得出一个完整帧就切出来（顺带从缓冲里删掉它），否则 None。"""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    fin, op = (b0 >> 7) & 1, b0 & 0x0F
    masked, ln = (b1 >> 7) & 1, b1 & 0x7F
    i = 2
    if ln == 126:
        if len(buf) < 4:
            return None
        ln = int.from_bytes(buf[2:4], "big")
        i = 4
    elif ln == 127:
        if len(buf) < 10:
            return None
        ln = int.from_bytes(buf[2:10], "big")
        i = 10
    if ln > _WS_MAX_FRAME:
        raise ValueError("WebSocket 帧异常大")
    key = None
    if masked:
        if len(buf) < i + 4:
            return None
        key = bytes(buf[i:i + 4])
        i += 4
    if len(buf) < i + ln:
        return None
    payload = bytes(buf[i:i + ln])
    if key:
        payload = bytes(c ^ key[n % 4] for n, c in enumerate(payload))
    del buf[:i + ln]
    return fin, op, payload


def _ws_connect(client_id: str, timeout: float = 10.0):
    """握手 + 返回（socket, 已经多读到的那几个字节）。"""
    s = socket.create_connection(("127.0.0.1", COMFY_PORT), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (f"GET /ws?clientId={client_id} HTTP/1.1\r\n"
           f"Host: 127.0.0.1:{COMFY_PORT}\r\n"
           "Upgrade: websocket\r\n"
           "Connection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n")
    s.sendall(req.encode("ascii"))
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            s.close()
            raise RuntimeError("握手时连接就被关了")
        head += chunk
        if len(head) > 65536:
            s.close()
            raise RuntimeError("握手响应异常")
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    if "101" not in line:
        s.close()
        raise RuntimeError(f"握手失败: {line}")
    return s, bytearray(head.split(b"\r\n\r\n", 1)[1])


def _ws_handle(job: dict, d: dict) -> None:
    t = d.get("type")
    data = d.get("data") or {}
    if t == "progress":
        try:
            job["steps"] = {"step": int(data.get("value") or 0), "total": int(data.get("max") or 0)}
        except (TypeError, ValueError):
            pass
    elif t == "executing":
        node = data.get("node")
        if node is None:            # 整个 prompt 跑完
            return
        ph = _PHASE_BY_NODE.get(str(node))
        if ph:
            _set_phase(job, ph)     # 作业已结束/已取消时 _set_phase 会自己拒绝
    elif t in ("execution_interrupted", "execution_error"):
        job["steps"] = None


def _ws_progress_loop(job: dict, stop: threading.Event) -> None:
    sock = None
    buf = bytearray()
    for attempt in range(5):
        if stop.is_set():
            return
        try:
            sock, buf = _ws_connect(job["client_id"])
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 4:
                print(f"[ws] 连不上 ComfyUI 的 /ws，进度环退回「只显示秒数」：{type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
                return
            stop.wait(2.0)
    if sock is None:
        return
    job["_ws_ready"] = True
    sock.settimeout(1.0)
    frag, frag_op = b"", 0
    try:
        while not stop.is_set():
            got = _ws_take_frame(buf)
            if got is None:
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                continue
            fin, op, payload = got
            if op == 0x9:                       # ping → pong（不回会被服务端掐）
                _ws_send(sock, 0xA, payload)
                continue
            if op == 0x8:                       # close
                return
            if op == 0xA:
                continue
            if op == 0x0:                       # continuation
                frag += payload
                if not fin:
                    continue
                op, payload = frag_op, frag
                frag, frag_op = b"", 0
            elif not fin:                       # 分片的开头
                frag, frag_op = payload, op
                continue
            if op not in (0x1, 0x2) or not payload:
                continue
            try:
                d = json.loads(payload.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(d, dict):
                _ws_handle(job, d)
    except Exception as e:  # noqa: BLE001 进度挂了不算任务失败
        print(f"[ws] 进度监听结束：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------- 作业状态 ----------
# 同一时刻只跑一个作业（ComfyUI 的 8188 也只吃得下一个）。job 跑完**不丢**：
# 前端要读它拿结果/错误，直到下一次任务开始才被顶掉。
_LOCK = threading.Lock()
_JOB: dict | None = None


class _Cancelled(Exception):
    """用户点了取消。"""


def _is_busy() -> bool:
    with _LOCK:
        return bool(_JOB and _JOB.get("active"))


def _set_phase(job: dict, phase: str, error: str | None = None, force: bool = False) -> None:
    # 迟到的 ws 事件不许把已经结束/取消的作业改回去（force=True 只给作业线程自己用）
    if not force and job.get("phase") in ("完成", "失败", "已取消") and phase != job["phase"]:
        return
    job["phase"] = phase
    if error is not None:
        job["error"] = str(error)[:600]


def _new_job(kind: str, params: dict) -> dict | None:
    """建任务并起线程；已经有任务在跑就返回 None。"""
    global _JOB
    with _LOCK:
        if _JOB and _JOB.get("active"):
            return None
        job = {
            "id": uuid.uuid4().hex[:12], "kind": kind, "phase": "准备中", "started": time.time(),
            "note": "", "steps": None, "peak_mib": None, "index": 1, "count": int(params.get("count") or 1),
            "results": [], "error": None, "active": True, "cancel": False,
            "client_id": uuid.uuid4().hex, "qid": None, "download": None, "_ws_ready": False,
        }
        _JOB = job
    threading.Thread(target=_run_job, args=(job, params), daemon=True, name=f"music-{kind}").start()
    return job


def _job_public(job: dict) -> dict:
    steps = job.get("steps")
    return {"id": job["id"], "kind": job["kind"], "phase": job["phase"], "started": job["started"],
            "elapsed": round(time.time() - job["started"], 1), "note": job.get("note") or "",
            "steps": dict(steps) if steps else None, "index": job.get("index"), "count": job.get("count"),
            "peak_mib": job.get("peak_mib"), "results": list(job.get("results") or []),
            "error": job.get("error")}


def _public_job() -> dict | None:
    with _LOCK:
        job = _JOB
        return _job_public(job) if job else None


def _busy() -> dict | None:
    with _LOCK:
        job = _JOB
        if not job or not job.get("active"):
            return None
        steps = job.get("steps")
        return {"id": job["id"], "kind": job["kind"], "phase": job["phase"], "started": job["started"],
                "elapsed": round(time.time() - job["started"], 1), "index": job.get("index"),
                "count": job.get("count"), "note": job.get("note") or "",
                "steps": dict(steps) if steps else None}


def _download_info() -> dict | None:
    with _LOCK:
        job = _JOB
        if not job or job["kind"] != "download" or not job.get("download"):
            return None
        return dict(job["download"])


# ---------- 日志（进度 note 的原材料）----------
# tqdm 的行只落在服务日志里（ABC / 语义采样、KSampler 的 it/s）。日志可能不存在、
# 也可能被重定向中的进程占着 → 一律 try 掉，读不到就当没这回事。
_NOTE_RE = re.compile(r"it/s|token/s|%\||YuE2|loading", re.I)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _decode_line(b: bytes) -> str:
    """ComfyUI 的日志**两种编码混着写**：模型/采样那几行按本地代码页（cp936），带 ANSI 色的行是 UTF-8。
    整块按一种解码必有一半乱码（tqdm 的方块条就是），所以**逐行**定编码：先试严格 UTF-8，
    严格失败才按 cp936 兜底（纯 ASCII 行两者一致，选哪个都对）。"""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp936", "replace")


def _tail_lines(path: Path, n: int, max_bytes: int = 65536) -> list[str]:
    """只回最后 n 行非空行：倒着读 64 KB 就够，别整文件读进来（日志能长到几十 MB）。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                data = fh.read()
                cut = data.find(b"\n")
                data = data[cut + 1:] if cut >= 0 else data   # 第一行可能是半截
            else:
                data = fh.read()
    except OSError:
        return []
    lines = [ln.rstrip() for ln in (_decode_line(x) for x in data.split(b"\n"))]
    lines = [ln for ln in lines if ln]
    return lines[-n:]


_BLOCKS = re.compile(r"[█▉▊▋▌▍▎▏░▒▓▁▂▃▄▅▆▇■□▪▫]+")
# tqdm 一行： 3%|▎  | 971/2250 [00:24<00:42, 51.92it/s]
_TQDM = re.compile(r"(?P<pct>\d{1,3})%\|[^|]*\|\s*(?P<done>[^\s/]+)\s*/\s*(?P<total>[^\s/]+)\s*"
                   r"\[(?P<el>[^<\]]*)<(?P<eta>[^,\]]*)(?:,\s*(?P<rate>[\d.]+)\s*(?P<unit>[A-Za-z]+/s))?")


def _fmt_note(ln: str) -> str:
    """日志尾行 → **一句给人看的短句**。

    tqdm 用 \\r 在同一行上反复覆盖，所以一"行"里塞着整段历史：取**最后一个**像进度的片段。
    再把 ANSI 色码、控制字符和 tqdm 那条**字符画**进度条（▉ 方块 + `[00:24<00:42, 51.9it/s]`）
    一起拿掉，只留数字 —— 状态栏里显示的是一条画出来的进度条，不是一串方块。"""
    ln = _ANSI.sub("", ln)
    frags = [s.strip() for s in ln.split("\r") if s.strip()]
    hit = [s for s in frags if _NOTE_RE.search(s)] or frags
    if not hit:
        return ""
    s = hit[-1]
    m = _TQDM.search(s)
    if m:
        head = "".join(ch for ch in _BLOCKS.sub("", s[:m.start()]) if ch >= " ").strip(" |:·")
        # 只留**别处没有的**信息：百分比与计数在状态栏的条和"采样 x / y"里已经有了，
        # 这里给速度与 ETA（两个都缺就退回计数）
        bits = []
        if m.group("rate"):
            bits.append(f"{m.group('rate')} {m.group('unit')}")
        eta = m.group("eta").strip()
        if eta and eta != "?" and any(c.isdigit() for c in eta):   # tqdm 还没测出速度时是 `?`
            bits.append("还剩 " + eta)
        if not bits:
            bits = [f"{m.group('pct')}%", f"{m.group('done')} / {m.group('total')}"]
        body = " · ".join(bits)
        return (head + " " + body if head else body)[:120]
    clean = "".join(ch for ch in _BLOCKS.sub("", s) if ch >= " " or ch == "\t").strip()
    return clean[:120]


def log_note() -> str:
    """日志里最后一条像进度的行。"""
    for p in (LOG, LOG_ERR):
        for ln in reversed(_tail_lines(p, 120)):
            if _NOTE_RE.search(ln):
                note = _fmt_note(ln)
                if note:
                    return note
    return ""


def log_tail(n: int) -> list[str]:
    lines = [_fmt_note(ln) for ln in (_tail_lines(LOG, 400) + _tail_lines(LOG_ERR, 200))]
    lines = [ln for ln in lines if ln]
    picked = [ln for ln in lines if _NOTE_RE.search(ln)]
    return (picked or lines)[-n:]


# ---------- 显存 ----------
_GPU_CACHE = {"t": 0.0, "used": None, "total": None}


def gpu_mib() -> tuple[int | None, int | None]:
    """(已用, 总量) MiB；没有 nvidia-smi 就 (None, None)，不报错。"""
    now = time.time()
    if now - _GPU_CACHE["t"] < 1.5:
        return _GPU_CACHE["used"], _GPU_CACHE["total"]
    used = total = None
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run([exe, "--query-gpu=memory.used,memory.total",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=_NO_WINDOW).stdout or ""
            first = out.strip().splitlines()[0]
            a, b = [x.strip() for x in first.split(",")[:2]]
            used, total = int(a), int(b)
        except Exception:  # noqa: BLE001
            used = total = None
    _GPU_CACHE.update(t=now, used=used, total=total)
    return used, total


def _sample_peak(job: dict) -> None:
    used, _total = gpu_mib()
    if used is None:
        return
    if job.get("peak_mib") is None or used > job["peak_mib"]:
        job["peak_mib"] = used


# ---------- 权重 / 产物 ----------
def ckpt_info() -> dict:
    present = CKPT.is_file()
    size = CKPT.stat().st_size if present else 0
    return {"name": CKPT_NAME, "present": present, "complete": present and size == CKPT_BYTES,
            "bytes": size, "expected_bytes": CKPT_BYTES, "sha256": CKPT_SHA256, "path": str(CKPT)}


def _ckpt_ready() -> bool:
    return CKPT.is_file() and CKPT.stat().st_size == CKPT_BYTES


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_FFPROBE: str | None | bool = False


def _ffprobe() -> str | None:
    global _FFPROBE
    if _FFPROBE is False:
        _FFPROBE = shutil.which("ffprobe")
    return _FFPROBE  # type: ignore[return-value]


def _audio_seconds(path: Path) -> float | None:
    exe = _ffprobe()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)],
                             capture_output=True, text=True, timeout=30,
                             creationflags=_NO_WINDOW).stdout.strip()
        return round(float(out.splitlines()[0]), 2) if out else None
    except Exception:  # noqa: BLE001
        return None


_DUR_CACHE: dict[str, tuple[float, int, float | None]] = {}
_DUR_LOCK = threading.Lock()
SONG_SCAN_LIMIT = 300
SECONDS_BUDGET = 20          # 每次 /api/state 最多新量 20 首的时长，剩下的下一轮再补（别把面板卡住）


def _song_kind(name: str) -> str | None:
    low = name.lower()
    if low.endswith(".flac"):
        return "song"
    if low.endswith(".abc"):
        return "plan"
    return None


_PAIR_TS = re.compile(r"^(?:song|plan)_(\d{8}_\d{6})$")


def _pair_key(stem: str) -> str:
    """同一首的所有产物共用一个 key：新产物就是文件名主干（`生日歌`）；老的 `song_<ts>`/`plan_<ts>` 归到 `<ts>`。"""
    m = _PAIR_TS.match(stem)
    return m.group(1) if m else stem


def _group_files(key: str) -> list[Path]:
    """这一首的全部产物：`<key>.flac/.abc/.json`，外加老前缀的 `song_<key>` / `plan_<key>`。"""
    stems = {key, f"song_{key}", f"plan_{key}"}
    return [p for s in sorted(stems) for ext in (".flac", ".abc", ".json")
            if (p := SONGS / f"{s}{ext}").is_file()]


def list_songs() -> list[dict]:
    """产物按**一首歌一行**分组：音频 / 乐谱 / 参数三件算同一首（面板的历史列表就是这么显示的）。"""
    if not SONGS.is_dir():
        return []
    groups: dict[str, dict] = {}
    stats: dict[str, tuple[float, int]] = {}
    try:
        entries = list(SONGS.iterdir())
    except OSError:
        return []
    for p in entries:
        try:
            if not p.is_file():
                continue
            kind = _song_kind(p.name)
            if not kind:
                continue
            st = p.stat()
        except OSError:
            continue
        stats[p.name] = (st.st_mtime, st.st_size)
        g = groups.setdefault(_pair_key(p.stem), {"stem": _pair_key(p.stem), "song": None,
                                                  "plan": None, "bytes": None, "mtime": 0.0})
        g[kind] = p.name
        if kind == "song" or not g["bytes"]:
            g["bytes"] = st.st_size                  # 有音频就报音频的大小
        g["mtime"] = max(g["mtime"], st.st_mtime)
    items = list(groups.values())
    for it in items:
        it["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(it["mtime"]))
        it["seconds"] = None
        anchor = it["song"] or it["plan"]
        it["meta"] = _meta_path(Path(anchor).stem).is_file()
    items.sort(key=lambda d: d["mtime"], reverse=True)
    items = items[:SONG_SCAN_LIMIT]
    budget = SECONDS_BUDGET
    for it in items:
        if not it["song"]:
            continue
        with _DUR_LOCK:
            hit = _DUR_CACHE.get(it["song"])
        st = stats.get(it["song"])
        if hit and st and hit[0] == st[0] and hit[1] == st[1]:
            it["seconds"] = hit[2]
            continue
        if budget <= 0:
            continue
        budget -= 1
        sec = _audio_seconds(SONGS / it["song"])
        if st:
            with _DUR_LOCK:
                _DUR_CACHE[it["song"]] = (st[0], st[1], sec)
        it["seconds"] = sec
    return items


def safe_file(name: str, exts: tuple[str, ...]) -> Path:
    """只认 songs_dir 里的**裸文件名** + 后缀白名单（防穿越）。"""
    raw = str(name or "").replace("\\", "/").strip()
    base = raw.rsplit("/", 1)[-1]
    if not base or base != raw or ".." in base:
        raise ValueError("非法文件名")
    root = SONGS.resolve()
    p = (root / base).resolve()
    if p.parent != root or p.suffix.lower() not in exts:
        raise ValueError("非法文件名")
    return p


def _unique(dest: Path) -> Path:
    """同秒重名就加 _2、_3（连出多首 / 快速两跑）。"""
    if not dest.exists():
        return dest
    for i in range(2, 100):
        cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        if not cand.exists():
            return cand
    return dest.with_name(f"{dest.stem}_{int(time.time() * 1000) % 100000}{dest.suffix}")


_BAD_LABEL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_label(name: str, fallback: str) -> str:
    """用户给的**歌名** → 能当文件名的主干：保留中文，换掉 Windows 非法字符与首尾的点/空格，
    空就用 fallback（`song_<时间戳>` 那种）。不在这里防重名 —— 那是 `_unique()` 的事。"""
    s = _BAD_LABEL.sub("_", str(name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" ._")
    s = re.sub(r"_{2,}", "_", s)[:60].strip(" ._")
    if not s:
        return fallback
    if s.upper() in _WIN_RESERVED:      # CON.flac 这种在 Windows 上建不出来
        s = "_" + s
    return s


def _pair_stem(stem: str) -> str | None:
    """老产物的配对：`song_<ts>` ↔ `plan_<ts>`（新产物是同名，不需要这一步）。"""
    if stem.startswith("song_"):
        return "plan_" + stem[5:]
    if stem.startswith("plan_"):
        return "song_" + stem[5:]
    return None


def _meta_path(stem: str) -> Path:
    """这一首的参数文件：先看同名 `<stem>.json`，再按老前缀配对找一次。找不到也回首选路径。"""
    own = SONGS / f"{stem}.json"
    if own.is_file():
        return own
    alt = _pair_stem(stem)
    if alt:
        c = SONGS / f"{alt}.json"
        if c.is_file():
            return c
    return own


def _write_meta(stem: str, data: dict) -> bool:
    """把这一首的参数写成 `<stem>.json`（面板那页「歌词 / 参数」读的就是它）。
    写不进去**不算任务失败**：只记一笔、前端那一页显示"没存参数"。"""
    try:
        (SONGS / f"{stem}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        return True
    except OSError as e:  # noqa: BLE001
        print(f"[meta] 写不了 {stem}.json：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return False


def _entry_audio(entry: dict) -> Path | None:
    """从 /history 的 outputs 里找带 audio 的节点 → <output_dir>\\<subfolder>\\<filename>。"""
    for node_out in (entry.get("outputs") or {}).values():
        for a in (node_out.get("audio") or []):
            fn = str(a.get("filename") or "")
            if not fn:
                continue
            return OUT / str(a.get("subfolder") or "") / fn
    return None


def _entry_text(entry: dict) -> str | None:
    for node_out in (entry.get("outputs") or {}).values():
        if "text" in node_out:
            t = node_out["text"]
            if isinstance(t, list):
                return str(t[0]) if t else ""
            return str(t)
    return None


# ---------- 作业主体 ----------
def _run_job(job: dict, params: dict) -> None:
    stop = threading.Event()
    try:
        if job["kind"] == "download":
            _run_download(job)
        else:
            ok, msg, _sec = start_comfy()
            if not ok:
                raise RuntimeError(msg)
            # 进度监听要在 /prompt 之前起来（同一个 client_id）。它自己吞异常，挂了只影响进度环。
            threading.Thread(target=_ws_progress_loop, args=(job, stop), daemon=True,
                             name="music-ws").start()
            if job["kind"] == "plan":
                _run_plan(job, params)
            else:
                _run_songs(job, params)
    except _Cancelled:
        _set_phase(job, "已取消", force=True)
    except Exception as e:  # noqa: BLE001
        txt = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
        _set_phase(job, "失败", error=txt, force=True)
    else:
        _set_phase(job, "完成", force=True)
    finally:
        stop.set()
        with _LOCK:
            job["active"] = False
        print(f"[job] {job['id']} {job['kind']} → {job['phase']}"
              + (f"：{job['error']}" if job.get("error") else ""), file=sys.stderr, flush=True)


def _submit_wait(job: dict, graph: dict, timeout: float) -> tuple[dict, float]:
    """提交一个图并等它跑完。返回（history 里那一项, 用了多少秒）。

    body 必须是 UTF-8 字节（中文歌词/风格会坏在代码页上），轮询 1.5 s 一次。
    """
    for _ in range(30):          # 等 ws 握上手（最多 3 s）；握不上也照常出歌
        if job.get("_ws_ready") or not job.get("active"):
            break
        time.sleep(0.1)
    try:
        resp = _api("/prompt", {"prompt": graph, "client_id": job["client_id"]}, timeout=120)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"ComfyUI 没接收这次任务（{e.code}）：{detail}") from None
    qid = resp.get("prompt_id")
    if not qid:
        raise RuntimeError("ComfyUI 没给出任务号（可能刚重启过，再点一次）")
    job["qid"] = str(qid)
    t0 = time.time()
    while True:
        if job.get("cancel"):
            raise _Cancelled()
        if time.time() - t0 > timeout:
            raise RuntimeError("等 ComfyUI 出结果等太久了（可能卡住或显存不够），再试一次")
        time.sleep(1.5)
        if job.get("cancel"):
            raise _Cancelled()
        _sample_peak(job)
        note = log_note()
        if note:
            job["note"] = note
        try:
            hist = _api(f"/history/{qid}", timeout=20)
        except Exception:  # noqa: BLE001 ComfyUI 忙/重启中：接着等
            continue
        entry = hist.get(qid)
        if not entry:
            continue
        st = entry.get("status") or {}
        if st.get("status_str") == "error":
            msgs = " | ".join(str(m) for m in (st.get("messages") or []))
            raise RuntimeError("ComfyUI 报错：" + msgs[-800:])
        return entry, time.time() - t0


def _run_songs(job: dict, p: dict) -> None:
    seed0 = int(p["seed"])
    for i in range(int(p["count"])):
        if job.get("cancel"):
            raise _Cancelled()
        job["index"] = i + 1
        job["steps"] = None
        job["note"] = ""
        job["peak_mib"] = None
        _set_phase(job, "准备中", force=True)
        seed = (seed0 + i) if seed0 >= 0 else random.randint(0, 2 ** 31 - 2)
        g = graph_song(p["style"], p["lyrics"], p["seconds"], seed, p["steps"], p["mode"])
        entry, wall = _submit_wait(job, g, timeout=1800.0)
        src = _entry_audio(entry)
        if src is None or not src.is_file():
            raise RuntimeError("ComfyUI 这次没给出音频文件（看看它的输出目录，或者是不是显存不够/被中断了）")
        SONGS.mkdir(parents=True, exist_ok=True)
        label = safe_label(p.get("label"), f"song_{time.strftime('%Y%m%d_%H%M%S')}")
        dest = _unique(SONGS / f"{label}.flac")
        shutil.copy2(src, dest)          # 拷贝，不动 ComfyUI 自己的 output\audio\
        # 同一次生成的 ABC 也落一份，**同名**（<stem>.flac ↔ <stem>.abc）→ 底部才出切换滑块
        abc = _entry_text(entry)
        plan_dest = None
        if abc and abc.strip():
            plan_dest = _unique(SONGS / f"{dest.stem}.abc")
            plan_dest.write_text(abc, encoding="utf-8", newline="\n")
        # **这一首的全部参数**写成 <stem>.json：面板那页「歌词 / 参数」读它（也是"能不能清空创作页"的依据）
        have_meta = _write_meta(dest.stem, {
            "v": 1, "kind": "song", "name": dest.name, "audio": dest.name,
            "plan": plan_dest.name if plan_dest else None, "label": (p.get("label") or "").strip(),
            "style": p["style"], "lyrics": p["lyrics"], "seed": seed, "mode": p["mode"],
            "seconds": p["seconds"], "steps": p["steps"], "count": p["count"], "index": i + 1,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "wall": round(wall, 1),
            "peak_mib": job.get("peak_mib"), "comfy_file": src.name, "meta": True})
        job["results"].append({"name": dest.name, "seconds": _audio_seconds(dest),
                               "wall": round(wall, 1), "peak_mib": job.get("peak_mib"),
                               "meta": have_meta})
        if plan_dest is not None:
            job["results"].append({"name": plan_dest.name, "seconds": None, "wall": None,
                                   "peak_mib": None, "text": abc, "meta": have_meta})
        print(f"[job] {job['id']} [{i + 1}/{p['count']}] {dest.name} "
              f"{wall:.1f}s 峰值 {job.get('peak_mib')} MiB", file=sys.stderr, flush=True)
        if job.get("cancel"):
            raise _Cancelled()


def _run_plan(job: dict, p: dict) -> None:
    job["peak_mib"] = None
    _set_phase(job, "乐谱规划中", force=True)
    seed = int(p["seed"]) if int(p["seed"]) >= 0 else random.randint(0, 2 ** 31 - 2)
    entry, wall = _submit_wait(job, graph_plan(p["style"], p["lyrics"], seed, p["mode"]), timeout=600.0)
    text = _entry_text(entry)
    if not text or not text.strip():
        raise RuntimeError("ComfyUI 这次没给出乐谱文本（再点一次试试）")
    SONGS.mkdir(parents=True, exist_ok=True)
    dest = _unique(SONGS / f"{safe_label(p.get('label'), f'plan_{time.strftime("%Y%m%d_%H%M%S")}')}.abc")
    dest.write_text(text, encoding="utf-8", newline="\n")     # UTF-8 无 BOM
    have_meta = _write_meta(dest.stem, {
        "v": 1, "kind": "plan", "name": dest.name, "audio": None, "plan": dest.name,
        "label": (p.get("label") or "").strip(), "style": p["style"], "lyrics": p["lyrics"],
        "seed": seed, "mode": p["mode"], "seconds": None, "steps": None, "count": 1, "index": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "wall": round(wall, 1),
        "peak_mib": job.get("peak_mib"), "comfy_file": None, "meta": True})
    job["results"].append({"name": dest.name, "seconds": None, "wall": round(wall, 1),
                           "peak_mib": job.get("peak_mib"), "text": text, "meta": have_meta})
    print(f"[job] {job['id']} plan → {dest.name} ({wall:.1f}s)", file=sys.stderr, flush=True)


def _run_download(job: dict) -> None:
    """3.96 GB 的东西别用 urllib 下：curl.exe -L --fail -C - 断点续传，进度按文件大小轮询。"""
    _set_phase(job, "下载中", force=True)
    CKPT.parent.mkdir(parents=True, exist_ok=True)
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("找不到 curl.exe（Windows 10+ 自带；或把 curl 放进 PATH）")
    curl_log = LOG.parent / (LOG.name + ".download.log")     # curl 自己的话落这儿，出错好查
    try:
        with open(curl_log, "ab") as fh:
            proc = subprocess.Popen([curl, "-L", "--fail", "-C", "-", "-o", str(CKPT), CKPT_URL],
                                    stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                    cwd=str(CKPT.parent), creationflags=_NO_WINDOW | _NEW_GROUP,
                                    close_fds=True)
    except OSError as e:
        raise RuntimeError(f"起 curl 失败: {e}") from None

    def record(recv: int, sha: bool | None = None) -> None:
        job["download"] = {"received": recv, "total": CKPT_BYTES,
                           "percent": round(min(recv / CKPT_BYTES, 1.0), 4), "sha_ok": sha}

    record(CKPT.stat().st_size if CKPT.is_file() else 0)
    while proc.poll() is None:
        if job.get("cancel"):
            proc.kill()
            raise _Cancelled()
        record(CKPT.stat().st_size if CKPT.is_file() else 0)
        time.sleep(1.0)
    rc = proc.returncode
    if rc != 0:
        raise RuntimeError(f"curl 退出码 {rc}（已下的部分留着，再点一次会从断点续传）")
    size = CKPT.stat().st_size if CKPT.is_file() else 0
    record(size)
    if size != CKPT_BYTES:
        raise RuntimeError(f"下载没下完（已下 {size} 字节 / 共 {CKPT_BYTES}），再点一次会接着下")
    digest = _sha256(CKPT)
    record(size, digest == CKPT_SHA256)
    if digest != CKPT_SHA256:
        raise RuntimeError(f"权重校验没过（算出来 {digest[:12]}…，期望 {CKPT_SHA256[:12]}…）"
                           f"—— 大概是下载坏了，删掉这个文件再点一次下载")


# ---------- 参数校验 ----------
def _parse_params(body: dict, plan: bool = False) -> dict:
    style = str(body.get("style") or "").strip()
    lyrics = str(body.get("lyrics") or "")
    # 歌名（可选）：只是给文件起名与写进参数文件，留空 = 落盘时按时间戳起
    label = re.sub(r"\s+", " ", str(body.get("name") or "").strip())[:60]
    if not style:
        raise ValueError("风格不能为空")
    if not lyrics.strip():
        raise ValueError("歌词不能为空")
    mode = str(body.get("mode") or "full").strip().lower()
    if mode not in ("full", "melody", "off"):
        raise ValueError("mode 只能是 full / melody / off")
    try:
        seed = int(body.get("seed", -1))
    except (TypeError, ValueError):
        raise ValueError("seed 要是整数（<0 表示随机）") from None
    if plan:
        return {"style": style, "lyrics": lyrics, "seed": seed, "mode": mode, "count": 1,
                "label": label, "seconds": None, "steps": None}
    try:
        seconds = float(body.get("seconds", 150))
    except (TypeError, ValueError):
        raise ValueError("seconds 要是数字") from None
    if seconds < 10:
        raise ValueError("seconds 不能小于 10")
    try:
        steps = int(body.get("steps", 32))
        count = int(body.get("count", 1))
    except (TypeError, ValueError):
        raise ValueError("steps / count 要是整数") from None
    if not 1 <= steps <= 80:
        raise ValueError("steps 要在 1–80 之间")
    if not 1 <= count <= 4:
        raise ValueError("count 要在 1–4 之间")
    return {"style": style, "lyrics": lyrics, "seconds": seconds, "seed": seed,
            "steps": steps, "count": count, "mode": mode, "label": label}


# ---------- 自动调参：提示词 → 歌词 + 参数（可选，走大模型） ----------

def _autotune_system() -> str:
    rows = "\n".join(f"{k}: " + "、".join(PRESETS.get(k, [])) for k in ("曲风", "人声", "乐器", "BPM"))
    return (
        "你是音乐制作人。用户用一句话描述想要的歌，你把它变成可以直接用的一组参数。\n"
        "**只输出一个 JSON 对象**：不要解释、不要 markdown 代码块、不要任何多余文字。\n"
        "输出结构照下面这个例子（值按用户的话改，字段一个都不能少）：\n"
        '{"name":"生日歌","曲风":"民谣","人声":"女声","乐器":"木吉他","BPM":"88 BPM",'
        '"seconds":90,"steps":32,"count":1,"mode":"full",'
        '"lyrics":"[Verse]\\n第一句\\n第二句\\n\\n[Chorus]\\n副歌一句","style_note":"一句中文，说明你为什么这么选"}\n'
        "字段说明：\n"
        "- name：给这首歌起个**短名字**（2-12 字，跟用户的语言），会当文件名用；"
        "别带路径、后缀、标点符号。\n"
        "- 曲风/人声/乐器/BPM：**逐字**从下面清单里挑；清单里实在没有合适的就给空字符串 \"\"。\n"
        "- seconds：歌曲时长**上限**（10-900 秒）。用户说了时长就按他说的；没说就按歌词量给 90-150。\n"
        "- steps：质量步数（1-80），没说就 32；用户要更精细可以 40-60。\n"
        "- count：出几首（1-4），没说就 1。\n"
        "- mode：要不要先出 ABC 乐谱 —— full=出谱、melody=只要旋律、off=不出谱；没说就 full。\n"
        "- lyrics：歌词，按 [Verse] / [Chorus]（需要时 [Bridge] / [Outro]）分段，一行一句；\n"
        "  **语言跟随用户的描述**（中文描述就写中文歌词）；主题、情绪、长短贴着描述来；\n"
        "  长度别超过 seconds 秒唱得完的量；不要写解释或作者注释。\n"
        "- style_note：一句话中文，给用户看你为什么这么选。\n"
        "四组预设的合法取值（逐字用，不要自造）：\n" + rows
    )


def call_llm(prompt: str, system: str) -> str:
    """打配置的大模型（任何 OpenAI 兼容端点）。key 只从环境变量 / 注册表读，请求按需走 LLM_PROXY。"""
    if not llm_ready():
        raise RuntimeError(llm_why())
    payload = {"model": LLM_MODEL,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": prompt}],
               "temperature": 0.7, "max_tokens": LLM_MAX_TOKENS}
    req = urllib.request.Request(LLM_BASE + "/chat/completions",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "Authorization": "Bearer " + _llm_key()})
    with _LLM_OPENER.open(req, timeout=LLM_TIMEOUT) as r:
        d = json.loads(r.read().decode("utf-8"))
    try:
        txt = str(d["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"大模型返回的内容看不懂（{type(e).__name__}），再试一次") from None
    if not txt.strip():
        raise RuntimeError("大模型这次没给正文（思考型模型把 token 花在思考上了 —— 调大 studio.json 的 llm.max_tokens）")
    return txt


def parse_llm_json(txt: str) -> dict:
    """模型爱裹 ```json 围栏、也爱在前后加一句话 —— 都剥掉，只取第一个 { 到最后一个 }。"""
    t = txt.strip()
    t = re.sub(r"^```[A-Za-z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < i:
        raise RuntimeError("大模型没按要求给出结果（返回的不是 JSON）：" + txt[:160])
    try:
        obj = json.loads(t[i:j + 1])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"大模型给的 JSON 解不开（{e}），再试一次") from None
    return obj if isinstance(obj, dict) else {}


def _num(v, lo: int, hi: int):
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def normalize_autotune(raw: dict) -> tuple[dict, list[str]]:
    """只回**合法**的改动：预设值必须逐字落在清单里，否则丢掉并记进 unmatched（前端会提示"没对上"）。"""
    ch: dict = {}
    miss: list[str] = []
    for k in ("曲风", "人声", "乐器", "BPM"):
        v = str(raw.get(k) or "").strip()
        if v and v in PRESETS.get(k, []):
            ch[k] = v
        else:
            miss.append(k)
    for src, key, lo, hi in (("seconds", "seconds", 10, 900), ("steps", "steps", 1, 80),
                             ("count", "count", 1, 4)):
        n = _num(raw.get(src), lo, hi)
        if n is not None:
            ch[key] = n
    mode = str(raw.get("mode") or "").strip().lower()
    if mode in ("full", "melody", "off"):
        ch["mode"] = mode
    ly = str(raw.get("lyrics") or "").strip()
    if ly:
        ch["lyrics"] = ly
    nm = re.sub(r"\s+", " ", str(raw.get("name") or "").strip())[:60]
    if nm:
        ch["name"] = nm
    note = str(raw.get("style_note") or "").strip()
    if note:
        ch["note"] = note
    return ch, miss


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "YueStudio/1.0"

    def log_message(self, fmt, *args):        # 别把每个请求都刷到控制台
        pass

    # --- 输出助手 ---
    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _fail(self, code: int, err: str, msg: str):
        self._json({"ok": False, "error": err, "message": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            d = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"请求体不是合法 JSON: {e}") from None
        return d if isinstance(d, dict) else {}

    def _file(self, path: Path, ctype: str, cache: str = "no-store"):
        try:
            data = path.read_bytes()
        except OSError as e:
            return self._fail(500, "io_error", f"读不到文件：{e}")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _send_file(self, path: Path, ctype: str):
        """带 Range 的文件发送：浏览器播放 + 拖动进度条都靠它。"""
        try:
            size = path.stat().st_size
        except OSError as e:
            return self._fail(404, "not_found", f"读不到文件：{e}")
        start, end, code = 0, max(size - 1, 0), 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:                                   # bytes=-N = 最后 N 字节
                    start = max(0, size - int(m.group(2)))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        length = max(end - start + 1, 0)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        remaining = length
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    # --- 路由 ---
    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            path = u.path
            if path in ("/", "/index.html"):
                if not PAGE.is_file():
                    return self._fail(404, "not_found", "找不到 yue2_studio.html")
                return self._file(PAGE, "text/html; charset=utf-8")

            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return

            if path == "/api/state":
                online, version = _comfy_stats()
                used, total = gpu_mib()
                return self._json({
                    "ok": True, "panel_port": PANEL_PORT, "comfy_port": COMFY_PORT,
                    "comfy_online": online, "comfy_version": version,
                    "autostart": COMFY_AUTOSTART,
                    "songs_dir": str(SONGS), "config_path": str(ROOT / "studio.json"),
                    "ckpt": ckpt_info(),
                    "gpu": {"used_mib": used, "total_mib": total},
                    "presets": PRESETS,
                    "llm": {"ready": llm_ready(), "model": LLM_MODEL, "key_env": LLM_KEY_ENV,
                            "why": llm_why()},
                    "songs": list_songs(),
                    "busy": _busy(),
                })

            if path == "/api/status":
                pending = 0
                try:
                    q = _api("/queue", timeout=5)
                    pending = len(q.get("queue_pending") or [])
                except Exception:  # noqa: BLE001 ComfyUI 没起也没关系
                    pending = 0
                return self._json({"ok": True, "job": _public_job(),
                                   "queue": {"pending": pending},
                                   "download": _download_info()})

            if path == "/api/log":
                qs = urllib.parse.parse_qs(u.query)
                try:
                    n = max(1, min(int((qs.get("lines") or ["6"])[0]), 200))
                except (TypeError, ValueError):
                    n = 6
                return self._json({"ok": True, "lines": log_tail(n)})

            if path.startswith("/vendor/"):
                name = path[len("/vendor/"):]
                base = name.rsplit("/", 1)[-1]
                p = VENDOR / base
                if base != name or not base.endswith(".js") or not p.is_file():
                    return self._fail(404, "not_found", "没有这个文件")
                return self._file(p, "text/javascript; charset=utf-8", cache="max-age=86400")

            if path == "/api/meta":
                # 这一首的参数文件（<stem>.json）。老产物（比这版早生成的）没有 → meta:null，
                # 前端那一页就只显示文件本身的信息，不编内容。
                qs = urllib.parse.parse_qs(u.query)
                raw = (qs.get("name") or [""])[0]
                try:
                    p = safe_file(raw if raw.lower().endswith((".flac", ".abc", ".json"))
                                  else raw + ".flac", (".flac", ".abc", ".json"))
                except ValueError as e:
                    return self._fail(404, "not_found", str(e))
                mp = _meta_path(p.stem)
                if not mp.is_file():
                    return self._json({"ok": True, "meta": None})
                try:
                    return self._json({"ok": True, "meta": json.loads(mp.read_text(encoding="utf-8"))})
                except (OSError, ValueError) as e:  # noqa: BLE001
                    return self._fail(500, "bad_meta", f"这一首的参数读不出来（文件可能坏了）：{e}")

            if path == "/song":
                qs = urllib.parse.parse_qs(u.query)
                try:
                    p = safe_file((qs.get("name") or [""])[0], (".flac",))
                except ValueError as e:
                    return self._fail(404, "not_found", str(e))
                if not p.is_file():
                    return self._fail(404, "not_found", "没有这个文件")
                return self._send_file(p, "audio/flac")

            if path == "/text":
                qs = urllib.parse.parse_qs(u.query)
                try:
                    p = safe_file((qs.get("name") or [""])[0], (".abc",))
                except ValueError as e:
                    return self._fail(404, "not_found", str(e))
                if not p.is_file():
                    return self._fail(404, "not_found", "没有这个文件")
                return self._send_file(p, "text/plain; charset=utf-8")

            return self._fail(404, "not_found", "未知路径")
        except Exception as e:  # noqa: BLE001
            self._fail(500, "internal", f"{type(e).__name__}: {e}")

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            try:
                body = self._body()
            except ValueError as e:
                return self._fail(400, "bad_request", str(e))

            if path in ("/api/generate", "/api/plan"):
                if _is_busy():
                    return self._fail(409, "busy", "有任务在跑，等它完（或者先点取消）")
                kind = "plan" if path == "/api/plan" else "song"
                try:
                    params = _parse_params(body, plan=(kind == "plan"))
                except ValueError as e:
                    return self._fail(400, "bad_request", str(e))
                if not COMFY_AUTOSTART and not comfy_online():
                    return self._fail(503, "offline", "ComfyUI 没在跑，而配置里关了自动拉起")
                if not _ckpt_ready():
                    return self._fail(503, "no_ckpt", "YuE2 权重还没就绪，先点下载（3.96 GB）")
                job = _new_job(kind, params)
                if job is None:
                    return self._fail(409, "busy", "有任务在跑，等它完（或者先点取消）")
                return self._json({"ok": True}, 202)

            if path == "/api/autotune":
                if not llm_ready():
                    return self._fail(503, "no_llm",
                                      f"没配大模型：studio.json 的 llm，或环境变量 {LLM_KEY_ENV}")
                prompt = str(body.get("prompt") or "").strip()
                if not prompt:
                    return self._fail(400, "bad_request", "提示词不能为空")
                try:
                    raw = parse_llm_json(call_llm(prompt, _autotune_system()))
                    ch, miss = normalize_autotune(raw)
                except Exception as e:  # noqa: BLE001 —— 大模型的错一律回给前端显示
                    return self._fail(502, "llm_failed", str(e))
                # **只回参数，不提交**：生成与否永远由用户在页面上点
                return self._json({"ok": True, "changes": ch, "unmatched": miss})

            if path == "/api/cancel":
                with _LOCK:
                    job = _JOB if (_JOB and _JOB.get("active")) else None
                    if job is not None:
                        job["cancel"] = True
                        job["phase"] = "已取消"
                if job is not None:
                    try:
                        _api("/interrupt", {}, timeout=10, method="POST")
                    except Exception as e:  # noqa: BLE001 没任务/没在线也别报错
                        print(f"[cancel] /interrupt 没发成功：{type(e).__name__}: {e}",
                              file=sys.stderr, flush=True)
                return self._json({"ok": True})

            if path == "/api/start":
                ok, msg, sec = start_comfy()
                if ok:
                    return self._json({"ok": True, "seconds": round(sec, 1)})
                return self._json({"ok": False, "message": msg})

            if path == "/api/download":
                if _is_busy():
                    return self._json({"ok": False, "message": "有任务在跑（出歌或下载中），等它完"})
                if _ckpt_ready():
                    return self._json({"ok": False, "message": "权重已经完整，不用下"})
                if not (shutil.which("curl.exe") or shutil.which("curl")):
                    return self._json({"ok": False, "message": "找不到 curl.exe"})
                if _new_job("download", {}) is None:
                    return self._json({"ok": False, "message": "有任务在跑（出歌或下载中），等它完"})
                return self._json({"ok": True})

            if path == "/api/open_dir":
                # 历史页那个「打开目录」：在资源管理器里打开产物目录，并把它**提到前台**
                # （后台进程开的窗口默认只在任务栏闪 —— 见 _open_path 的说明）
                if not SONGS.is_dir():
                    return self._fail(404, "not_found", f"产物目录还没建：{SONGS}")
                try:
                    raised = _open_path(SONGS)
                except OSError as e:  # noqa: BLE001
                    return self._fail(500, "io_error", f"打不开 {SONGS}：{e}")
                return self._json({"ok": True, "dir": str(SONGS), "raised": raised})

            if path == "/api/rename":
                # 改一首的**歌名** = 改文件名主干：<旧>.flac/.abc/.json → <新>.*，并把 .json 里的
                # label 与两个产物名一起改掉（「属性」页读的就是它，所以属性里会立刻跟着变）。
                raw = str(body.get("name") or "").strip().replace("\\", "/")
                base = raw.rsplit("/", 1)[-1]
                if not base or base != raw or ".." in base:
                    return self._fail(404, "not_found", "非法文件名")
                key = _pair_key(Path(base).stem if "." in base else base)
                files = _group_files(key)
                if not files:
                    return self._fail(404, "not_found", "没有这一首")
                new = safe_label(str(body.get("to") or ""), "")
                if not new:
                    return self._fail(400, "bad_name", "新名字不能为空（也别只有非法字符）")
                if new == key:
                    return self._json({"ok": True, "stem": new, "files": [], "same": True})
                # 不能撞上**别的**一首 —— 判据是分组 key（老命名的 song_<ts>/plan_<ts> 与
                # 新命名 <ts> 是**同一首**：光看文件名会漏，实测漏过一次，两首被并成一张卡）
                new_key = _pair_key(new)
                try:
                    others = {_pair_key(p.stem) for p in SONGS.iterdir()
                              if p.is_file() and _song_kind(p.name)}
                except OSError:
                    others = set()
                if new_key in others and new_key != key:
                    return self._fail(409, "exists", f"已经有叫「{new_key}」的一首了，换个名字")
                moved: list[str] = []
                for p in files:
                    try:
                        p.rename(SONGS / f"{new}{p.suffix}")
                    except OSError as e:  # noqa: BLE001
                        return self._fail(500, "io_error", f"{p.name} 改不动：{e}")
                    moved.append(f"{new}{p.suffix}")
                with _DUR_LOCK:
                    for n in (f"{key}.flac", f"{new}.flac"):
                        _DUR_CACHE.pop(n, None)
                mp = SONGS / f"{new}.json"
                if mp.is_file():
                    try:
                        m = json.loads(mp.read_text(encoding="utf-8"))
                    except (OSError, ValueError):  # noqa: BLE001 参数文件坏了也别把改名回滚
                        m = None
                    if isinstance(m, dict):
                        m["label"] = new
                        for k in ("name", "audio"):
                            if str(m.get(k) or "").lower().endswith(".flac"):
                                m[k] = f"{new}.flac"
                        for k in ("name", "plan"):
                            if str(m.get(k) or "").lower().endswith(".abc"):
                                m[k] = f"{new}.abc"
                        try:
                            mp.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8", newline="\n")
                        except OSError as e:  # noqa: BLE001
                            print(f"[rename] 参数文件写不回去 {mp.name}：{e}", file=sys.stderr, flush=True)
                return self._json({"ok": True, "stem": new, "files": moved})

            if path == "/api/delete":
                raw = str(body.get("name") or "").strip().replace("\\", "/")
                base = raw.rsplit("/", 1)[-1]
                if not base or base != raw or ".." in base:
                    return self._fail(404, "not_found", "非法文件名")
                if "." not in base:
                    # 没后缀 = 删这一首的**全部产物**（音频 + 乐谱 + 参数），前端历史里那个 × 就是这条
                    key = _pair_key(base)
                    gone = []
                    for f in _group_files(key):
                        try:
                            f.unlink()
                            gone.append(f.name)
                        except OSError as e:  # noqa: BLE001
                            return self._fail(500, "io_error", f"删不掉 {f.name}：{e}")
                    with _DUR_LOCK:
                        for n in gone:
                            _DUR_CACHE.pop(n, None)
                    return self._json({"ok": True, "deleted": gone})
                try:
                    p = safe_file(base, (".flac", ".abc", ".json"))
                except ValueError as e:
                    return self._fail(404, "not_found", str(e))
                if not p.is_file():
                    return self._fail(404, "not_found", "没有这个文件")
                try:
                    p.unlink()
                except OSError as e:
                    return self._fail(500, "io_error", f"删不掉：{e}")
                with _DUR_LOCK:
                    _DUR_CACHE.pop(p.name, None)
                # 同名的那对产物（歌 + 谱）**一件都不剩**了，才把参数文件也收走 ——
                # 否则删了歌、留着谱，谱那页的参数就空了。老前缀产物要连配对的那个 stem 一起看。
                stems = {p.stem}
                alt = _pair_stem(p.stem)
                if alt:
                    stems.add(alt)
                alive = [q for s in stems for q in (SONGS / f"{s}.flac", SONGS / f"{s}.abc") if q.is_file()]
                if not alive:
                    for s in stems:
                        m = SONGS / f"{s}.json"
                        try:
                            m.unlink(missing_ok=True)
                        except OSError as e:  # noqa: BLE001
                            print(f"[delete] 参数文件删不掉 {m.name}：{e}", file=sys.stderr, flush=True)
                return self._json({"ok": True, "deleted": [p.name]})

            return self._fail(404, "not_found", "未知路径")
        except Exception as e:  # noqa: BLE001
            self._fail(500, "internal", f"{type(e).__name__}: {e}")


# ---------- 启动 ----------
def config_dump() -> dict:
    return {"panel_port": PANEL_PORT, "comfy_port": COMFY_PORT, "root": str(ROOT),
            "config_path": str(ROOT / "studio.json"),
            "comfy_root": str(COMFY), "output_dir": str(OUT), "songs_dir": str(SONGS),
            "comfy_python": str(PY), "comfy_cmd": COMFY_CMD, "comfy_log": str(LOG),
            "comfy_autostart": COMFY_AUTOSTART, "presets": PRESETS,
            "ckpt": ckpt_info(), "ckpt_url": CKPT_URL}


def _find_explorer_window(want: str) -> int:
    """按标题找一个 Explorer **文件夹**窗口（class = CabinetWClass），返回 HWND，找不到回 0。

    为什么不能只 `os.startfile`：面板是个**后台进程**，Windows 的前台锁不允许它把新窗口提到前面，
    于是窗口只作为一枚闪烁的任务栏按钮出现（实测用户就是这么看到的）。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    want = want.strip().lower()
    hits: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value.strip().lower()
            if t and (t == want or t.endswith(want) or want in t):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls, 64)
                if cls.value == "CabinetWClass":       # 只认文件夹窗口，别把浏览器标签页抓来
                    hits.append(hwnd)
                    return False
        return True

    user32.EnumWindows(proc(cb), 0)
    return hits[0] if hits else 0


def _raise_window(hwnd: int) -> None:
    """把窗口提到前台。后台进程直接 `SetForegroundWindow` 会被前台锁挡掉 ——
    标准做法是先把**本线程的输入挂到当前前台窗口的线程上**，再提，完事摘掉。"""
    import ctypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_me = kernel32.GetCurrentThreadId()
    attached = bool(tid_fg) and tid_fg != tid_me
    try:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, True)
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE（最小化过也能拉回来）
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)


def _open_path(p: Path) -> bool:
    """在文件管理器里打开一个目录，并尽量**把它提到前台**。返回"提到了吗"。

    非 Windows 就交给系统的 opener；Windows 上先 startfile，再等窗口出现、把它拉到前台
    （最多等 ~2 s；没等到也算打开了，只是可能还在任务栏闪）。"""
    if os.name != "nt":
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    try:
        os.startfile(str(p))                   # noqa: S606
    except OSError:
        subprocess.Popen(["explorer.exe", str(p)], creationflags=_NO_WINDOW)
    want = p.name or str(p)
    for _ in range(20):
        time.sleep(0.1)
        hwnd = _find_explorer_window(want)
        if hwnd:
            _raise_window(hwnd)
            return True
    return False


def _panel_answering(port: int) -> bool:
    """这个端口上已经有面板在答话吗。

    Windows 上 `allow_reuse_address` 允许**两个进程同时 bind 同一端口** —— 谁接请求看运气，
    于是会出现"新代码起了、但答话的是那个老进程"（实测：面板明明改了，`/api/state` 还是老行为）。
    服务起来之前先探一下，别让自己变成第二个隐形实例。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    global PANEL_PORT
    ap = argparse.ArgumentParser(description="乐坊 YuE2 Studio面板（纯标准库，零第三方依赖）")
    ap.add_argument("--port", type=int, default=None, help="面板端口（默认 8190；优先于 YUE2_PANEL_PORT）")
    ap.add_argument("--print-config", action="store_true", help="打印解析后的配置 JSON 后退出")
    a = ap.parse_args(argv)
    if a.port:
        PANEL_PORT = int(a.port)
    if a.print_config:
        print(json.dumps(config_dump(), ensure_ascii=False, indent=2))
        return 0

    if _panel_answering(PANEL_PORT):
        print(f"已经有面板在 {PANEL_PORT} 上跑了（同一端口能 bind 两个进程，答话的会是老的那个）。\n"
              f"要么先用它：http://127.0.0.1:{PANEL_PORT}\n"
              f"要么先停掉：powershell -File yue2_studio.ps1 -Stop", file=sys.stderr)
        return 1
    if not COMFY.is_dir():
        # 不因为"找不到 ComfyUI"就拒绝启动：起得来，状态栏才能告诉你它没在线
        print(f"提示: 没找到 ComfyUI 目录 {COMFY}（面板照常起，出歌会报错；在 studio.json 里改 comfy_root）",
              file=sys.stderr)
    online, version = _comfy_stats(ttl=0)
    srv = ThreadingHTTPServer(("127.0.0.1", PANEL_PORT), Handler)
    srv.daemon_threads = True
    print(f"乐坊 YuE2 Studio: http://127.0.0.1:{PANEL_PORT}", flush=True)
    print(f"  comfy_root : {COMFY}", flush=True)
    print(f"  songs_dir  : {SONGS}", flush=True)
    print("  ComfyUI    : " + (f"在线 v{version}  http://127.0.0.1:{COMFY_PORT}" if online
                              else f"没在跑（{COMFY_PORT}）" +
                                   ("，出歌时会自动拉起" if COMFY_AUTOSTART else "，且配置里关了自动拉起")),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
