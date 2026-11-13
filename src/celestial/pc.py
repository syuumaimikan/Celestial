"""PC操作(Windows): アプリを開く、文字を打つ、キーやマウスを操作する、画面を撮る、PCの状態を調べる。

標準ライブラリ(ctypes)だけで実装している。Windows 以外では SUPPORTED が False になり、道具として出てこない。
"""

from __future__ import annotations

import csv
import ctypes
import io
import os
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zlib
from pathlib import Path

SUPPORTED = sys.platform == "win32"


class PCError(Exception):
    pass


if SUPPORTED:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdiplus = ctypes.WinDLL("gdiplus")
    ole32 = ctypes.WinDLL("ole32")
    try:
        user32.SetProcessDPIAware()  # 画面の画素とマウス座標を一致させる
    except OSError:
        pass

    ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    user32.GetForegroundWindow.restype = wintypes.HWND
    gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipCreateBitmapFromHBITMAP.argtypes = [wintypes.HBITMAP, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipSaveImageToStream.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    ole32.CreateStreamOnHGlobal.argtypes = [ctypes.c_void_p, wintypes.BOOL, ctypes.POINTER(ctypes.c_void_p)]
    ole32.GetHGlobalFromStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

_KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE = 0x0002, 0x0004
_VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "ctrl": 0x11, "control": 0x11, "shift": 0x10,
    "alt": 0x12, "win": 0x5B, "windows": 0x5B, "printscreen": 0x2C,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1, "stopmedia": 0xB2,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}


def _require() -> None:
    if not SUPPORTED:
        raise PCError("PC操作は Windows でのみ使えます")


def parse_keys(keys: str) -> list[list[int]]:
    """"ctrl+s" や "ctrl+a ctrl+c" を、同時押しする仮想キーコードの列に直す。"""
    combos = []
    for combo in keys.replace(",", " ").split():
        codes = []
        for name in combo.lower().split("+"):
            if name in _VK:
                codes.append(_VK[name])
            elif len(name) == 1 and (name.isascii() and name.isalnum()):
                codes.append(ord(name.upper()))
            else:
                raise PCError(f"分からないキーです: {name}(例: enter, tab, esc, ctrl+s, alt+f4, f5)")
        if codes:
            combos.append(codes)
    if not combos:
        raise PCError("押すキーが指定されていません")
    return combos


def _send(inputs: list) -> None:
    array = (_INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT)) != len(inputs):
        raise PCError("キーやマウスの操作を送れませんでした(管理者権限のアプリが前面にあると送れません)")


def _key(vk: int = 0, scan: int = 0, flags: int = 0):
    return _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, 0)))


def _mouse(flags: int, data: int = 0):
    return _INPUT(type=0, u=_INPUTUNION(mi=_MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)))


def foreground_title() -> str:
    _require()
    hwnd = user32.GetForegroundWindow()
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def _target(window: str | None) -> str:
    """入力の送り先を決める。window を指定したら前面に出し、本当に前面になったか確かめる。"""
    if window and window.strip():
        focus_window(window)
        title = foreground_title()
        if window.strip().lower() not in title.lower():
            raise PCError(f"「{window}」を前面に出せませんでした(いま前面にあるのは「{title or '不明'}」)。入力は送っていません")
        return title
    return foreground_title()


def type_text(text: str, window: str | None = None) -> str:
    _require()
    title = _target(window)
    if len(text) > 4000:
        raise PCError("一度に入力できるのは4000文字までです")
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            _send([_key(0x0D), _key(0x0D, flags=_KEYEVENTF_KEYUP)])
        elif ch == "\t":
            _send([_key(0x09), _key(0x09, flags=_KEYEVENTF_KEYUP)])
        else:
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):  # 絵文字などはサロゲートペアで2回に分けて送る
                unit = struct.unpack_from("<H", data, i)[0]
                _send([_key(scan=unit, flags=_KEYEVENTF_UNICODE), _key(scan=unit, flags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)])
        time.sleep(0.004)
    return f"{len(text)} 文字を入力しました(入力先: {title or '不明'})"


def press_keys(keys: str, window: str | None = None) -> str:
    _require()
    combos = parse_keys(keys)
    title = _target(window)
    for codes in combos:
        _send([_key(vk) for vk in codes] + [_key(vk, flags=_KEYEVENTF_KEYUP) for vk in reversed(codes)])
        time.sleep(0.08)
    return f"キーを押しました: {keys}(入力先: {title or '不明'})"


def click(x: int, y: int, button: str = "left") -> str:
    _require()
    button = (button or "left").lower()
    if button not in ("left", "right", "double"):
        raise PCError("button は left / right / double のいずれかです")
    width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if not (0 <= x < width and 0 <= y < height):
        raise PCError(f"画面の外です。画面は {width}×{height} です")
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    down, up = (0x0008, 0x0010) if button == "right" else (0x0002, 0x0004)
    for _ in range(2 if button == "double" else 1):
        _send([_mouse(down), _mouse(up)])
        time.sleep(0.06)
    return f"({x}, {y}) を{'右' if button == 'right' else 'ダブル' if button == 'double' else ''}クリックしました"


def scroll(amount: int) -> str:
    _require()
    _send([_mouse(0x0800, max(-20, min(20, amount)) * 120)])
    return f"スクロールしました: {amount}"


def volume(action: str) -> str:
    _require()
    action = (action or "").lower()
    if action not in ("up", "down", "mute"):
        raise PCError("action は up / down / mute のいずれかです")
    vk = _VK["volume" + action]
    for _ in range(1 if action == "mute" else 5):  # 1回で2%なので、5回で約10%
        _send([_key(vk), _key(vk, flags=_KEYEVENTF_KEYUP)])
    return {"up": "音量を約10%上げました", "down": "音量を約10%下げました", "mute": "消音を切り替えました"}[action]


def media(action: str) -> str:
    _require()
    keys = {"play_pause": "playpause", "next": "nexttrack", "previous": "prevtrack", "stop": "stopmedia"}
    if action not in keys:
        raise PCError("action は play_pause / next / previous / stop のいずれかです")
    vk = _VK[keys[action]]
    _send([_key(vk), _key(vk, flags=_KEYEVENTF_KEYUP)])
    return {"play_pause": "再生/一時停止を切り替えました", "next": "次の曲へ進めました",
            "previous": "前の曲へ戻しました", "stop": "再生を止めました"}[action]


def lock() -> str:
    _require()
    if not user32.LockWorkStation():
        raise PCError("画面をロックできませんでした")
    return "画面をロックしました"


def screen_size() -> tuple[int, int]:
    _require()
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def move(x: int, y: int) -> str:
    _require()
    user32.SetCursorPos(x, y)
    return f"マウスを ({x}, {y}) へ動かしました"


def drag(x1: int, y1: int, x2: int, y2: int) -> str:
    _require()
    user32.SetCursorPos(x1, y1)
    time.sleep(0.05)
    _send([_mouse(0x0002)])
    for i in range(1, 11):  # 途中を通らないとドラッグと認識しないアプリがある
        user32.SetCursorPos(x1 + (x2 - x1) * i // 10, y1 + (y2 - y1) * i // 10)
        time.sleep(0.015)
    _send([_mouse(0x0004)])
    return f"({x1}, {y1}) から ({x2}, {y2}) へドラッグしました"


def wait(seconds: float) -> str:
    seconds = max(0.0, min(10.0, seconds))
    time.sleep(seconds)
    return f"{seconds:g} 秒待ちました"


# ---- ウィンドウ --------------------------------------------------------------

def list_windows() -> list[tuple[int, str]]:
    _require()
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buffer = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buffer, 512)
            if buffer.value.strip() and buffer.value not in ("Program Manager",):
                found.append((hwnd, buffer.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def _find_window(title: str) -> tuple[int, str]:
    needle = title.strip().lower()
    if not needle:
        raise PCError("ウィンドウの名前が指定されていません")
    for hwnd, name in list_windows():
        if needle in name.lower():
            return hwnd, name
    raise PCError(f"「{title}」を含むウィンドウが見つかりません。list_windows で名前を確かめてください")


def focus_window(title: str) -> str:
    hwnd, name = _find_window(title)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # 他のアプリから前面を奪うには、直前に入力があったことにする必要がある(Alt を空押しする)
    _send([_key(0x12), _key(0x12, flags=_KEYEVENTF_KEYUP)])
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    return f"前面に出しました: {name}"


def close_window(title: str) -> str:
    hwnd, name = _find_window(title)
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE(アプリ側で保存の確認などが出ることがある)
    return f"閉じるよう依頼しました: {name}"


def open_target(target: str) -> str:
    _require()
    target = target.strip().strip('"')
    if not target:
        raise PCError("開くものが指定されていません")
    if target.lower().startswith(("http://", "https://")):
        webbrowser.open(target)
        return f"ブラウザで開きました: {target}"
    try:
        os.startfile(os.path.expanduser(target))  # アプリ名(notepad など)・ファイル・フォルダのどれでも開ける
    except OSError as e:
        raise PCError(f"開けませんでした: {target}({e.strerror or e})") from e
    time.sleep(1.5)  # 起動を少し待つ
    return f"開きました: {target}"


# ---- クリップボード ----------------------------------------------------------

def _powershell(command: str, stdin: str | None = None, timeout: float = 15) -> str:
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               "[Console]::OutputEncoding=[Text.Encoding]::UTF8; [Console]::InputEncoding=[Text.Encoding]::UTF8; " + command],
                              input=stdin.encode("utf-8") if stdin is not None else None, capture_output=True, timeout=timeout,
                              creationflags=0x08000000)  # コンソール窓を出さない
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PCError(f"操作に失敗しました: {e}") from e
    return proc.stdout.decode("utf-8", "replace")


def clipboard_read() -> str:
    _require()
    return _powershell("Get-Clipboard -Raw").rstrip("\r\n")


def clipboard_write(text: str) -> str:
    _require()
    _powershell("$t = [Console]::In.ReadToEnd(); Set-Clipboard -Value $t", stdin=text)
    return f"クリップボードに {len(text)} 文字を入れました"


# ---- 状態 --------------------------------------------------------------------

def system_info() -> str:
    lines = [f"時刻: {time.strftime('%Y-%m-%d %H:%M:%S')}", f"OS: {platform.system()} {platform.release()} ({platform.version()})",
             f"CPU: {platform.processor() or '不明'}、論理コア {os.cpu_count()}"]
    if SUPPORTED:
        class _MEMORY(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "avail", "totalPage", "availPage", "totalVirtual", "availVirtual", "availExt")]

        memory = _MEMORY()
        memory.dwLength = ctypes.sizeof(_MEMORY)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            lines.append(f"メモリ: {(memory.total - memory.avail) / 2**30:.1f} / {memory.total / 2**30:.1f} GB 使用中({memory.dwMemoryLoad}%)")

        class _POWER(ctypes.Structure):
            _fields_ = [("ac", ctypes.c_byte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte), ("saver", ctypes.c_byte),
                        ("life", wintypes.DWORD), ("full", wintypes.DWORD)]

        power = _POWER()
        if kernel32.GetSystemPowerStatus(ctypes.byref(power)):
            if power.flag & 128:
                lines.append("電源: AC(バッテリーなし)")
            elif power.percent <= 100:
                lines.append(f"バッテリー: {power.percent}%({'充電中' if power.ac == 1 else '放電中'})")
        drives = [f"{letter}:\\" for letter in "CDEFGHIJ" if os.path.exists(f"{letter}:\\")]
    else:
        drives = ["/"]
    for drive in drives:
        try:
            usage = shutil.disk_usage(drive)
            if not usage.total:
                continue
            lines.append(f"ディスク {drive} 空き {usage.free / 2**30:.0f} / {usage.total / 2**30:.0f} GB")
        except OSError:
            continue
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=8, creationflags=0x08000000 if SUPPORTED else 0).stdout.strip()
            if out:
                lines.append("GPU: " + out.splitlines()[0])
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "\n".join(lines)


def list_processes(limit: int = 25) -> str:
    _require()
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=20, creationflags=0x08000000).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PCError(f"一覧を取得できませんでした: {e}") from e
    totals: dict[str, list[int]] = {}
    for row in csv.reader(io.StringIO(out.decode("cp932", "replace"))):
        if len(row) >= 5:
            digits = "".join(ch for ch in row[4] if ch.isdigit())
            entry = totals.setdefault(row[0], [0, 0])
            entry[0] += int(digits or 0)
            entry[1] += 1
    top = sorted(totals.items(), key=lambda item: item[1][0], reverse=True)[:limit]
    return "\n".join(f"{name}  {kb / 1024:.0f} MB" + (f"  ({count} 個)" if count > 1 else "") for name, (kb, count) in top)


# ---- 画面 --------------------------------------------------------------------

def _png(width: int, height: int, bgra: bytes) -> bytes:
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[row * stride : (row + 1) * stride]) for row in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))


def capture(max_width: int | None = None, fmt: str = "png", quality: int = 70) -> tuple[bytes, int, int, int, int]:
    """主画面を PNG(fmt="jpeg" なら JPEG)にして (バイト列, 画像の幅, 高さ, 画面の幅, 高さ) を返す。

    max_width を指定すると、取り込みの時点で縮小する(GDI の縮小は速く、送る量も減る)。
    """
    _require()
    screen_w, screen_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    width, height = screen_w, screen_h
    if max_width and max_width < screen_w:
        width, height = max_width, max(1, screen_h * max_width // screen_w)
    screen = user32.GetDC(None)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    try:
        previous = gdi32.SelectObject(memory, bitmap)
        if (width, height) == (screen_w, screen_h):
            ok = gdi32.BitBlt(memory, 0, 0, width, height, screen, 0, 0, 0x00CC0020)  # SRCCOPY
        else:
            gdi32.SetStretchBltMode(memory, 4)  # HALFTONE: 縮小しても文字がつぶれにくい
            gdi32.SetBrushOrgEx(memory, 0, 0, None)
            ok = gdi32.StretchBlt(memory, 0, 0, width, height, screen, 0, 0, screen_w, screen_h, 0x00CC0020)
        if not ok:
            raise PCError("画面を取得できませんでした")
        gdi32.SelectObject(memory, previous)  # GDI+ に渡す前に、DC から外しておく
        if fmt == "jpeg":
            try:
                return _jpeg(bitmap, quality), width, height, screen_w, screen_h
            except (PCError, OSError):
                pass  # JPEG にできなければ PNG にする
        header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)  # 高さを負にすると上から下の順になる
        buffer = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(memory, bitmap, 0, height, buffer, header, 0) != height:
            raise PCError("画面を取得できませんでした")
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)
    return _png(width, height, buffer.raw), width, height, screen_w, screen_h


def screenshot(path: Path, max_width: int | None = None) -> tuple[int, int]:
    """主画面を PNG で保存し、(幅, 高さ) を返す。"""
    data, width, height, _, _ = capture(max_width)
    Path(path).write_bytes(data)
    return width, height


# ---- JPEG(GDI+ で作る。PNG より小さく速いので、スマホへの画面の送信に使う) ----------

_gdiplus_ready = False
_gdiplus_lock = threading.Lock()


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    u = uuid.UUID(text)
    return _GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


class _EncoderParameter(ctypes.Structure):
    _fields_ = [("Guid", _GUID), ("NumberOfValues", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("Value", ctypes.c_void_p)]


class _EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Parameter", _EncoderParameter * 1)]


_JPEG_CLSID = _guid("557CF401-1A04-11D3-9A73-0000F81EF32E")
_ENCODER_QUALITY = _guid("1D5BE4B5-FA4A-452D-9CDD-5DB35105E7EB")


def _start_gdiplus() -> None:
    global _gdiplus_ready
    with _gdiplus_lock:
        if _gdiplus_ready:
            return

        class _StartupInput(ctypes.Structure):
            _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
                        ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL)]

        token = ctypes.c_size_t()
        if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(_StartupInput(1, None, False, False)), None) != 0:
            raise PCError("画像の変換機能(GDI+)を使えませんでした")
        _gdiplus_ready = True


def _jpeg(hbitmap, quality: int) -> bytes:
    _start_gdiplus()
    image = ctypes.c_void_p()
    if gdiplus.GdipCreateBitmapFromHBITMAP(hbitmap, None, ctypes.byref(image)) != 0:
        raise PCError("画面を画像に変換できませんでした")
    stream = ctypes.c_void_p()
    try:
        if ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream)) != 0:
            raise PCError("画面を画像に変換できませんでした")
        value = ctypes.c_uint32(max(10, min(95, quality)))
        params = _EncoderParameters(1, (_EncoderParameter * 1)(_EncoderParameter(_ENCODER_QUALITY, 1, 4, ctypes.cast(ctypes.pointer(value), ctypes.c_void_p))))
        if gdiplus.GdipSaveImageToStream(image, stream, ctypes.byref(_JPEG_CLSID), ctypes.byref(params)) != 0:
            raise PCError("画面を JPEG にできませんでした")
        table = ctypes.cast(stream, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        seek = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_longlong, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulonglong))(table[5])
        size = ctypes.c_ulonglong()
        seek(stream, 0, 2, ctypes.byref(size))  # 末尾まで進めて、書き込んだ大きさを知る
        hglobal = ctypes.c_void_p()
        ole32.GetHGlobalFromStream(stream, ctypes.byref(hglobal))
        pointer = kernel32.GlobalLock(hglobal)
        try:
            return ctypes.string_at(pointer, size.value)
        finally:
            kernel32.GlobalUnlock(hglobal)
    finally:
        if stream:
            table = ctypes.cast(stream, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(stream)  # Release
        gdiplus.GdipDisposeImage(image)
