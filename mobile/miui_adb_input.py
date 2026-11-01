# -*- coding: UTF-8 -*-
"""MIUI 兼容层：将 uiautomator2 的触摸/按键注入改走 adb `input` 命令。

背景：MIUI 上即使开启了「USB 调试（安全设置）」，uia2 服务的
UiAutomation 触摸注入（TouchController.touchDown）仍可能抛
SecurityException（INJECT_EVENTS）。而 adb shell 的 `input tap/swipe/keyevent`
通道不受影响。本模块把设备实例上的触摸类方法替换为 adb 命令实现；
文本输入走 set_text（RPC，非触摸注入），不受影响。

所有命令复用同一条常驻 `adb shell` 连接：Windows 下每次新开
`adb shell` 要 ~170ms（进程创建 + adb 握手），点击频率只能到 ~6 次/秒；
常驻连接把单次点击往返压到 ~10-35ms，实测 ~30 次/秒。

通过环境变量 HATICKETS_MIUI_ADB_INPUT=1 显式启用，不影响默认行为。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

logger = logging.getLogger(__name__)

# u2 press() 接受的符号键名 → Android keycode 数字。
# adb `input keyevent` 只接受数字或 KEYCODE_XXX 全名，不认 "enter" 这类短名。
_U2_KEY_NAME_TO_KEYCODE = {
    "home": 3,
    "back": 4,
    "dpad_up": 19,
    "dpad_down": 20,
    "dpad_left": 21,
    "dpad_right": 22,
    "dpad_center": 23,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "camera": 27,
    "clear": 28,
    "a": 29,
    "b": 30,
    "c": 31,
    "d": 32,
    "e": 33,
    "f": 34,
    "g": 35,
    "h": 36,
    "i": 37,
    "j": 38,
    "k": 39,
    "l": 40,
    "m": 41,
    "n": 42,
    "o": 43,
    "p": 44,
    "q": 45,
    "r": 46,
    "s": 47,
    "t": 48,
    "u": 49,
    "v": 50,
    "w": 51,
    "x": 52,
    "y": 53,
    "z": 54,
    "comma": 55,
    "period": 56,
    "alt_left": 57,
    "alt_right": 58,
    "shift_left": 59,
    "shift_right": 60,
    "tab": 61,
    "space": 62,
    "sym": 63,
    "explorer": 64,
    "envelope": 65,
    "enter": 66,
    "del": 67,
    "grave": 68,
    "minus": 69,
    "equals": 70,
    "left_bracket": 71,
    "right_bracket": 72,
    "backslash": 73,
    "semicolon": 74,
    "apostrophe": 75,
    "slash": 76,
    "at": 77,
    "num": 78,
    "headsethook": 79,
    "focus": 80,
    "plus": 81,
    "menu": 82,
    "notification": 83,
    "search": 84,
    "app_switch": 187,
    "cut": 277,
    "copy": 278,
    "paste": 279,
}


def miui_adb_input_enabled() -> bool:
    return os.environ.get("HATICKETS_MIUI_ADB_INPUT", "").strip() == "1"


def _keycode_for(key) -> int:
    """把 u2 press() 的参数（数字或符号名）统一转成 Android keycode 数字。"""
    if isinstance(key, bool):  # pragma: no cover - 防御 bool 是 int 子类
        raise ValueError("keycode 不能是 bool")
    if isinstance(key, int):
        return key
    name = str(key).strip().lower()
    if name.isdigit():
        return int(name)
    if name in _U2_KEY_NAME_TO_KEYCODE:
        return _U2_KEY_NAME_TO_KEYCODE[name]
    # "KEYCODE_ENTER" 这类全名：去掉前缀后查表
    if name.startswith("keycode_"):
        short = name[len("keycode_"):]
        if short in _U2_KEY_NAME_TO_KEYCODE:
            return _U2_KEY_NAME_TO_KEYCODE[short]
    raise ValueError(f"无法识别的按键名: {key!r}")


class PersistentAdbShell:
    """常驻 `adb shell`：命令写入 stdin，由设备端 shell 顺序执行。

    单条命令执行仍需 ~30ms（设备端 `input` 命令本身的开销），
    但省去了 Windows 侧每次 ~170ms 的 adb 进程创建。
    """

    def __init__(self, serial: str):
        self._serial = serial
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        cmd = ["adb"]
        if self._serial:
            cmd += ["-s", self._serial]
        cmd += ["shell"]
        # 二进制管道：避免 Windows 文本模式把 \n 写成 \r\n。
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _ensure(self) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            try:
                self._connect()
            except OSError:
                return False
        return True

    def exec(self, cmd: str) -> None:
        """把命令写入常驻 shell 的 stdin（异步，由设备 shell 排队执行）。"""
        with self._lock:
            if not self._ensure():
                return
            try:
                self._proc.stdin.write((cmd + "\n").encode("ascii"))
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass  # 下次调用会重连

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None


_PSHELL = None


def get_persistent_shell(serial):
    global _PSHELL
    if _PSHELL is None:
        _PSHELL = PersistentAdbShell(serial)
    return _PSHELL


def apply_miui_adb_input(d, serial=None) -> bool:
    """Monkey-patch uiautomator2 device touch methods to adb shell input."""
    pshell = get_persistent_shell(serial)

    def _shell(cmd: str) -> None:
        pshell.exec(cmd)

    def _click(x, y):
        _shell(f"input tap {int(x)} {int(y)}")

    def _long_click(x, y, duration=0.5):
        _shell(
            f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} "
            f"{int(max(duration, 0.05) * 1000)}"
        )

    def _swipe(sx, sy, ex, ey, duration=0.3, **_kw):
        _shell(
            f"input swipe {int(sx)} {int(sy)} {int(ex)} {int(ey)} "
            f"{int(max(duration, 0.05) * 1000)}"
        )

    def _press(key, meta=None):
        _shell(f"input keyevent {_keycode_for(key)}")

    d.click = _click
    d.long_click = _long_click
    d.swipe = _swipe
    d.press = _press
    logger.info("已启用 MIUI ADB 输入兼容层（HATICKETS_MIUI_ADB_INPUT=1）")
    return True
