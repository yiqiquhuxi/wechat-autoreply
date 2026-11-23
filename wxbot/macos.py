"""macOS 底层能力：窗口定位、截图、OCR、鼠标键盘、剪贴板、Dock 角标、空闲检测。

只用系统公开接口，不碰微信进程。
"""
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

import AppKit
import Quartz
import Vision
from Foundation import NSURL
from PIL import Image

WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
WECHAT_OWNER_NAMES = ("微信", "WeChat")


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float
    wid: int = 0  # 窗口编号，用于单独截这个窗口（被挡住也能截）

    @property
    def right(self):
        return self.x + self.w

    @property
    def bottom(self):
        return self.y + self.h


@dataclass
class OcrLine:
    """一行 OCR 结果，坐标是相对截图区域左上角的「点」(pt)，不是像素。"""
    text: str
    x: float
    y: float
    w: float
    h: float
    conf: float

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2


# ---------- 窗口 ----------

def wechat_pid():
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(WECHAT_BUNDLE_ID)
    return apps[0].processIdentifier() if apps else None


def wechat_window():
    """返回微信主窗口在屏幕上的位置（pt）。找不到（最小化/没开）返回 None。"""
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    best = None
    for w in infos or []:
        if w.get("kCGWindowOwnerName") not in WECHAT_OWNER_NAMES or w.get("kCGWindowLayer") != 0:
            continue
        b = w["kCGWindowBounds"]
        r = Rect(b["X"], b["Y"], b["Width"], b["Height"], int(w.get("kCGWindowNumber", 0)))
        # 主窗口一般最大；过滤掉小弹窗
        if r.w < 500 or r.h < 400:
            continue
        if best is None or r.w * r.h > best.w * best.h:
            best = r
    return best


def frontmost_app():
    """当前最前面的 App。不用 NSWorkspace.frontmostApplication，它在无 runloop 的脚本里会过期。"""
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for w in infos or []:
        if w.get("kCGWindowLayer") == 0:
            return AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(w["kCGWindowOwnerPID"])
    return None


def _open_bundle(bundle_id):
    # 新版 macOS 不允许后台进程用 activateWithOptions 抢前台，会被静默忽略；open -b 走系统启动服务，可靠
    subprocess.run(["open", "-b", bundle_id], capture_output=True, timeout=10)


def activate(app):
    if app is not None and app.bundleIdentifier():
        _open_bundle(str(app.bundleIdentifier()))


def wechat_is_front():
    app = frontmost_app()
    return app is not None and str(app.bundleIdentifier() or "") == WECHAT_BUNDLE_ID


def activate_wechat(timeout=3.0):
    """把微信切到最前面，并确认切换成功。失败返回 False。"""
    if wechat_pid() is None:
        return False
    _open_bundle(WECHAT_BUNDLE_ID)
    end = time.time() + timeout
    while time.time() < end:
        if wechat_is_front() and wechat_window() is not None:
            return True
        time.sleep(0.15)
    return False


# ---------- 截图 + OCR ----------

def screenshot(rect: Rect):
    """截图，返回 (PIL.Image, scale)。scale = 像素/点，Retina 屏一般是 2。
    有窗口编号时只截这个窗口本身，被别的窗口挡住也不影响。"""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        if rect.wid:
            cmd = ["screencapture", "-x", "-o", "-l", str(rect.wid), path]
        else:
            cmd = ["screencapture", "-x", "-R", f"{int(rect.x)},{int(rect.y)},{int(rect.w)},{int(rect.h)}", path]
        subprocess.run(cmd, check=True)
        img = Image.open(path)
        img.load()
    finally:
        os.unlink(path)
    return img, img.width / rect.w


def full_screenshot():
    """整屏截图，返回 (img, scale)。右键菜单是独立的弹出窗口，按微信主窗口截图看不到它，
    只能整屏截。坐标单位是屏幕坐标（点）。"""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        subprocess.run(["screencapture", "-x", "-o", path], check=True)
        img = Image.open(path)
        img.load()
    finally:
        os.unlink(path)
    disp = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return img, img.width / disp.size.width


def ocr(img: Image.Image, scale: float):
    """用系统 Vision 框架识别中英文，返回 OcrLine 列表（坐标单位 pt）。"""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        img.save(path)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(["zh-Hans", "en-US"])
        req.setUsesLanguageCorrection_(False)
        handler.performRequests_error_([req], None)
        results = req.results() or []
    finally:
        os.unlink(path)

    w_pt, h_pt = img.width / scale, img.height / scale
    lines = []
    for obs in results:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        c = cands[0]
        bb = obs.boundingBox()  # 归一化坐标，原点在左下
        x = bb.origin.x * w_pt
        y = (1 - bb.origin.y - bb.size.height) * h_pt
        lines.append(OcrLine(str(c.string()), x, y, bb.size.width * w_pt, bb.size.height * h_pt, float(c.confidence())))
    lines.sort(key=lambda l: (round(l.y), l.x))
    return lines


# ---------- 鼠标键盘 ----------

_last_synth = 0.0       # 本程序最后一次模拟输入的时间
_user_idle_base = 0.0   # 那一刻用户已空闲的秒数
_guard_since = 0.0      # 从这个时间点开始，用户一动就中止
ABORT = threading.Event()  # 退出程序时置位，所有模拟输入立刻停止
should_stop = lambda: False  # 由 bot 设置：开关被关掉时返回 True


class UserBusy(Exception):
    """用户动了键盘鼠标 / 微信不在前台 / 程序被停止，本轮立即中止。"""


def begin_guard():
    """开始一段自动操作。之后只要用户碰了键盘鼠标，下一次模拟输入就会抛 UserBusy。"""
    global _guard_since
    _guard_since = time.time()


def user_interrupted():
    since = time.time() - max(_last_synth, _guard_since)
    return idle_seconds() + 0.3 < since


def _post(ev, guard=True):
    global _last_synth, _user_idle_base
    if guard:
        if ABORT.is_set() or should_stop():
            raise UserBusy("已暂停或退出")
        if user_interrupted():
            raise UserBusy("你动了键盘或鼠标")
    _user_idle_base = user_idle_seconds()
    _last_synth = time.time()
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def mouse_pos():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def move(x, y, guard=True):
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft), guard)


def click(x, y, double=False):
    move(x, y)
    time.sleep(0.05)
    times = 2 if double else 1
    for i in range(times):
        down = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft)
        up = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetIntegerValueField(down, Quartz.kCGMouseEventClickState, i + 1)
        Quartz.CGEventSetIntegerValueField(up, Quartz.kCGMouseEventClickState, i + 1)
        _post(down)
        _post(up)
        time.sleep(0.05)


def right_click(x, y):
    """右键（用于引用的菜单）。按下和抬起之间留一点时间，微信才会弹菜单。"""
    move(x, y)
    time.sleep(0.15)
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventRightMouseDown, (x, y), Quartz.kCGMouseButtonRight))
    time.sleep(0.08)
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventRightMouseUp, (x, y), Quartz.kCGMouseButtonRight))
    time.sleep(0.2)


def scroll_px(dy):
    """在鼠标当前位置发一次按像素的滚动。dy>0 往上（看更前面的内容），<0 往下。
    按「行」滚动时微信每次只挪一点点，所以用像素。"""
    _post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 1, int(dy)))


KEY_ESC = 53
KEY_V = 9
KEY_RETURN = 36


def key(code, cmd=False):
    for is_down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, is_down)
        if cmd:
            Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        _post(ev)
        time.sleep(0.03)


# ---------- 剪贴板 ----------

def clipboard_get():
    return AppKit.NSPasteboard.generalPasteboard().stringForType_(AppKit.NSPasteboardTypeString)


def clipboard_set(text):
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    if text is not None:
        pb.setString_forType_(text, AppKit.NSPasteboardTypeString)


# ---------- 状态 ----------

def dock_badge():
    """微信 Dock 图标角标上的总未读数。读不到返回 None，没有角标返回 0。"""
    try:
        out = subprocess.run(
            ["lsappinfo", "info", "-only", "StatusLabel", WECHAT_BUNDLE_ID],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None
    m = re.search(r'"label"="([^"]*)"', out)
    if not m:
        return 0
    label = m.group(1).strip()
    if not label:
        return 0
    digits = re.sub(r"\D", "", label)
    return int(digits) if digits else 1  # 「•」之类的红点按 1 算


def idle_seconds():
    """用户多少秒没碰键盘鼠标了。"""
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
    )


def user_idle_seconds():
    """用户本人多久没碰电脑。系统空闲计时会被本程序的模拟点击刷新，这里把那部分扣掉。"""
    idle = idle_seconds()
    since = time.time() - _last_synth
    if idle + 0.3 < since:  # 我们最后一次模拟之后，用户自己动过
        return idle
    return _user_idle_base + since


def permissions():
    import ApplicationServices as AS
    return {
        "辅助功能": bool(AS.AXIsProcessTrusted()),
        "屏幕录制": bool(Quartz.CGPreflightScreenCaptureAccess()),
    }
