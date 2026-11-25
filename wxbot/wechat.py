"""微信界面解析与操作：布局识别、会话列表、红点、聊天内容、点击与发送。

所有坐标单位都是 pt，并且相对微信窗口左上角；需要点屏幕时再加上窗口的 x/y。
"""
import hashlib
import logging
import re
import statistics
import time
from dataclasses import dataclass, asdict

from . import macos

log = logging.getLogger("wxbot")


@dataclass
class Layout:
    sidebar_w: float   # 最左侧图标栏宽度
    list_x: float      # 会话列表左边界
    list_w: float      # 会话列表宽度
    list_top: float    # 会话列表起始 y（搜索框下方）
    chat_x: float      # 聊天区左边界
    chat_w: float      # 聊天区宽度
    title_h: float     # 聊天标题栏高度
    input_top: float   # 输入区顶部 y
    win_w: float
    win_h: float

    def __str__(self):
        return ", ".join(f"{k}={v:.0f}" for k, v in asdict(self).items())


# ---------- 布局 ----------

def _edge_scores(gray, axis, band, span, step=2, thr=8, gap=2):
    """对每一列(axis='x')或每一行(axis='y')，统计「和相邻位置亮度差大于阈值」的比例。
    贯穿整片区域的分割线/背景色交界处，这个比例接近 1。"""
    px = gray.load()
    scores = {}
    for i in range(span[0] + gap, span[1]):
        hit = total = 0
        for j in range(band[0], band[1], step):
            a, b = (px[i, j], px[i - gap, j]) if axis == "x" else (px[j, i], px[j, i - gap])
            total += 1
            if abs(a - b) > thr:
                hit += 1
        scores[i] = hit / total if total else 0
    return scores


def _best(scores, lo, hi, default, min_score=0.85):
    cands = [(s, i) for i, s in scores.items() if lo <= i <= hi and s >= min_score]
    return max(cands)[1] if cands else default


def detect_layout(img, scale, win, overrides=None):
    """根据截图自动找分割线。overrides 里给了的值优先（用于手动校准）。"""
    w, h = int(win.w), int(win.h)
    gray = img.convert("L").resize((w, h))
    xs = _edge_scores(gray, "x", band=(int(h * 0.3), int(h * 0.8)), span=(30, min(w - 300, 700)))
    sidebar_w = _best(xs, 40, 120, 64)
    list_right = _best(xs, sidebar_w + 150, min(w - 300, 700), sidebar_w + 260)

    ys_chat = _edge_scores(gray, "y", band=(list_right + 20, w - 20), span=(30, h - 60))
    title_h = _best(ys_chat, 35, 110, 60)
    input_top = _best(ys_chat, h - 280, h - 80, h - 160)

    ys_list = _edge_scores(gray, "y", band=(sidebar_w + 10, list_right - 10), span=(30, 140))
    list_top = _best(ys_list, 40, 130, 64, min_score=0.6)

    lay = Layout(
        sidebar_w=sidebar_w, list_x=sidebar_w, list_w=list_right - sidebar_w, list_top=list_top,
        chat_x=list_right, chat_w=w - list_right, title_h=title_h, input_top=input_top,
        win_w=w, win_h=h,
    )
    for k, v in (overrides or {}).items():
        if v is not None and hasattr(lay, k):
            setattr(lay, k, float(v))
    return lay


# ---------- 会话列表 ----------

_TIME_RE = re.compile(r"^(\d{1,2}:\d{2}|昨天.*|前天|星期.|周.|\d{1,2}/\d{1,2}|\d{2,4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}月\d{1,2}日)$")


def find_avatars(img, scale, lay: Layout):
    """在会话列表左侧找头像方块，返回 [(top, bottom)]（相对窗口，pt）。
    做法：同一高度上，头像列的像素和最左边空白处的像素颜色不同；连续 28~56pt 高的一段就是一个头像。
    和同一行左边空白处比，而不是和固定背景色比，这样被选中（高亮）的那一行也能认出来。"""
    x0 = int(lay.list_x)
    sub = img.crop((int(x0 * scale), 0, int((x0 + 70) * scale), int(lay.win_h * scale)))
    sub = sub.convert("RGB").resize((70, int(lay.win_h)))
    px = sub.load()
    cols = (18, 28, 38)
    out, run_start = [], None
    for y in range(int(lay.list_top) - 50, int(lay.win_h)):
        if y < 0:
            continue
        ref = px[3, y]
        diff = sum(1 for c in cols if sum(abs(a - b) for a, b in zip(px[c, y], ref)) > 24)
        on = diff >= 2
        if on and run_start is None:
            run_start = y
        elif not on and run_start is not None:
            if 28 <= y - run_start <= 56:
                out.append((run_start, y))
            run_start = None
    return out


def parse_session_rows(lines, lay: Layout, avatars=None):
    """把会话列表整理成行：[{name, preview, y, h, top, bottom}]。
    有头像位置时按头像切行（最稳）；否则退回按右上角时间切行。"""
    region = [l for l in lines
              if lay.list_x + 50 < l.x < lay.chat_x - 4 and l.y > lay.list_top - 60 and l.y < lay.win_h - 4]
    texts = [l for l in region if not _TIME_RE.match(l.text.strip()) and not re.fullmatch(r"\d{1,3}\+?", l.text.strip())]
    rows = []
    if avatars:
        for top, bottom in avatars:
            inside = sorted((l for l in texts if top - 6 <= l.cy <= bottom + 2), key=lambda l: (l.y, l.x))
            if not inside:
                continue
            first = inside[0]
            same = [l for l in inside if abs(l.cy - first.cy) < max(8, first.h * 0.7)]
            name = min(same, key=lambda l: l.x)
            rest = [l for l in inside if l.y > name.y + name.h * 0.6]
            row = {"name": name.text.strip().lstrip("•·").strip(), "y": name.y, "h": name.h, "top": top, "bottom": bottom}
            if rest:
                row["preview"] = min(rest, key=lambda l: (l.y, l.x)).text
            tm = [l for l in region if _TIME_RE.match(l.text.strip()) and top - 8 <= l.cy <= bottom + 2]
            if tm:
                row["time"] = min(tm, key=lambda l: abs(l.cy - name.cy)).text.strip()
            rows.append(row)
        # 兜底：头像太暗（和深色背景接近）时认不出方块，但名称那一列有字，照样补成一行
        if rows:
            col_x = statistics.median(r_l.x for r_l in texts if any(abs(r_l.y - r["y"]) < 1 for r in rows))
            covered = lambda l: any(r["top"] - 6 <= l.cy <= r["bottom"] + 2 for r in rows)
            orphans = sorted((l for l in texts if not covered(l) and abs(l.x - col_x) < 24), key=lambda l: l.y)
            for l in orphans:
                if covered(l):  # 刚补的那一行的预览
                    continue
                top = l.y - 3
                row = {"name": l.text.strip().lstrip("•·").strip(), "y": l.y, "h": l.h,
                       "top": top, "bottom": top + 36, "inferred": True}
                below = [o for o in orphans if 8 < o.y - l.y < 30]
                if below:
                    row["preview"] = below[0].text
                tm = [l2 for l2 in region if _TIME_RE.match(l2.text.strip()) and 0 <= l2.cy - l.cy < 30]
                if tm:
                    row["time"] = tm[0].text.strip()
                rows.append(row)
            rows.sort(key=lambda r: r["top"])
        return rows
    times = sorted((l for l in region if _TIME_RE.match(l.text.strip()) and l.x > lay.list_x + lay.list_w * 0.6),
                   key=lambda l: l.y)
    for t in times:
        same = [l for l in texts if l.x + l.w < t.x and abs(l.cy - t.cy) < max(9, l.h * 0.8)]
        if not same:
            continue
        name = min(same, key=lambda l: l.x)
        below = [l for l in texts if 8 < l.y - name.y < 34 and l.x < t.x]
        row = {"name": name.text.strip().lstrip("•·").strip(), "y": name.y, "h": name.h,
               "top": name.y - 6, "bottom": name.y + 40}
        if below:
            row["preview"] = min(below, key=lambda l: l.x).text
        rows.append(row)
    return rows


def find_badges(img, scale, lay: Layout):
    """在头像区域找红色角标（未读数字或红点）。返回 [{x, y, r}]，坐标相对窗口。"""
    x0, x1 = int(lay.list_x), int(lay.list_x + 90)
    y0, y1 = int(lay.list_top), int(lay.win_h)
    sub = img.crop((int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale)))
    sw, sh = x1 - x0, y1 - y0
    sub = sub.convert("RGB").resize((sw, sh))
    px = sub.load()
    red = set()
    for x in range(sw):
        for y in range(sh):
            r, g, b = px[x, y]
            if r > 200 and g < 110 and b < 110:
                red.add((x, y))
    clusters = []
    while red:
        seed = red.pop()
        stack, pts = [seed], [seed]
        while stack:
            cx, cy = stack.pop()
            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if (nx, ny) in red:
                    red.remove((nx, ny))
                    stack.append((nx, ny))
                    pts.append((nx, ny))
        if len(pts) < 12:  # 太小的是噪点
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        if bh > 26 or bw > 40:  # 太大的不是角标（比如红色头像）
            continue
        cx = (min(xs) + max(xs)) / 2
        if cx < 34:  # 红点压在头像右上角，偏左的是头像图案本身
            continue
        clusters.append({"x": x0 + (min(xs) + max(xs)) / 2, "y": y0 + (min(ys) + max(ys)) / 2, "r": max(bw, bh) / 2})
    clusters.sort(key=lambda c: c["y"])
    return clusters


def attach_badges(rows, badges):
    """把红点对应到会话行上：红点压在头像右上角，所以落在头像上沿附近。"""
    out = []
    for row in rows:
        mine = [b for b in badges if row["top"] - 12 <= b["y"] <= row["top"] + (row["bottom"] - row["top"]) * 0.5]
        if mine:
            b = mine[0]
            out.append({**row, "badge_r": b["r"], "badge_y": b["y"]})
    return out


# ---------- 聊天内容 ----------

_GROUP_RE = re.compile(r"[（(]\s*\d+\s*[)）]\s*$")


def parse_chat(lines, lay: Layout):
    """解析右侧聊天区：标题、是否群聊、消息列表 [{text, me, sender}]（从上到下）。"""
    # 标题在聊天区最上面一行；下面可能还有「进群申请」之类的横幅，不能取最左边的
    title_lines = [l for l in lines if l.x >= lay.chat_x + 4 and l.y < lay.title_h
                   and l.cx < lay.chat_x + lay.chat_w * 0.7]
    title = ""
    if title_lines:
        top = min(title_lines, key=lambda l: l.y)
        same_row = sorted((l for l in title_lines if abs(l.cy - top.cy) < top.h * 0.6), key=lambda l: l.x)
        title = "".join(l.text for l in same_row).strip()

    body = [l for l in lines if l.x >= lay.chat_x + 4 and lay.title_h + 4 < l.y and l.y + l.h < lay.input_top - 2]
    body.sort(key=lambda l: (l.y, l.x))
    chat_right = lay.chat_x + lay.chat_w
    tagged = []
    for l in body:
        left_gap = l.x - lay.chat_x
        right_gap = chat_right - (l.x + l.w)
        if left_gap < right_gap - 40:
            side = "in"
        elif right_gap < left_gap - 40:
            side = "me"
        else:
            side = "sys"  # 居中：时间、系统提示、撤回提示
        tagged.append((side, l))

    is_group = bool(_GROUP_RE.search(title))

    # 群聊里对方的昵称在气泡上方，和头像右边对齐；气泡里的文字因为有内边距，会再往右缩进约 12pt。
    # 用这个缩进区分「昵称」和「消息」，比字号可靠。
    sender_ids = set()
    if is_group:
        ins = [l for side, l in tagged if side == "in"]
        if ins:
            name_x = min(l.x for l in ins)
            for i, l in enumerate(ins):
                if abs(l.x - name_x) > 4 or len(l.text) > 24:
                    continue
                nxt = ins[i + 1] if i + 1 < len(ins) else None
                if nxt is not None and 6 <= nxt.x - name_x <= 24 and 0 < nxt.y - l.y < 50:
                    sender_ids.add(id(l))

    # 相邻同侧的行合并为一条消息；遇到昵称行就开始新的一条
    blocks = []
    sender = None
    for side, l in tagged:
        if id(l) in sender_ids:
            sender = l.text.strip()
            blocks.append(None)  # 断开，防止和上一条合并
            continue
        last = blocks[-1] if blocks else None
        if last and last["side"] == side and side != "sys" and l.y - last["bottom"] < max(10, l.h * 0.9):
            last["lines"].append(l)
            last["bottom"] = l.y + l.h
        else:
            blocks.append({"side": side, "lines": [l], "bottom": l.y + l.h,
                           "sender": sender if side == "in" else None})
            if side == "me":
                # 我方发言后，对方再说话时昵称行会重新出现；先清掉，免得出错人
                sender = None

    messages = []
    for b in blocks:
        if b is None or b["side"] == "sys":
            continue
        xs = [l.x for l in b["lines"]]
        xe = [l.x + l.w for l in b["lines"]]
        messages.append({
            "text": "\n".join(l.text for l in b["lines"]).strip(),
            "me": b["side"] == "me",
            "sender": b["sender"],
            "y": b["lines"][0].y,
            "x": min(xs),
            "w": max(xe) - min(xs),
        })
    return {"title": title, "is_group": is_group, "messages": messages}


# ---------- 操作 ----------

class WeChatUI:
    def __init__(self, layout_overrides=None, settle=0.6):
        self.overrides = layout_overrides or {}
        self.settle = settle
        self.win = None
        self.lay = None

    def snapshot(self):
        """截一次图，返回 (img, scale, lines)。顺带刷新窗口位置和布局。"""
        self.win = macos.wechat_window()
        if self.win is None:
            return None
        img, scale = macos.screenshot(self.win)
        if self.lay is None or (self.lay.win_w, self.lay.win_h) != (int(self.win.w), int(self.win.h)):
            self.lay = detect_layout(img, scale, self.win, self.overrides)
        return img, scale, macos.ocr(img, scale)

    def _abs(self, x, y):
        return self.win.x + x, self.win.y + y

    def _ensure_front(self):
        """点击/滚动之前确认微信在最前面，否则会点到别的窗口上。"""
        if not macos.wechat_is_front():
            raise macos.UserBusy("微信不在最前面")
        w = macos.wechat_window()
        if w is None:
            raise macos.UserBusy("微信窗口不见了")
        if (w.x, w.y, w.w, w.h) != (self.win.x, self.win.y, self.win.w, self.win.h):
            raise macos.UserBusy("微信窗口被移动或缩放了")

    # ----- 会话列表滚动：都用「截图前后对比」判断有没有动，不靠估计 -----

    def list_hash(self):
        """会话列表区域的画面指纹（不做 OCR，很快）。"""
        img, scale = macos.screenshot(self.win)
        box = (int(self.lay.list_x * scale), int(self.lay.list_top * scale),
               int(self.lay.chat_x * scale) - 2, int(self.lay.win_h * scale) - 2)
        return hashlib.md5(img.crop(box).tobytes()).hexdigest()

    def chat_hash(self):
        """聊天内容区（标题下、输入框上）的画面指纹。"""
        img, scale = macos.screenshot(self.win)
        box = (int(self.lay.chat_x * scale), int(self.lay.title_h * scale),
               int(self.lay.win_w * scale) - 2, int(self.lay.input_top * scale))
        return hashlib.md5(img.crop(box).tobytes()).hexdigest()

    def scroll_chat_to_bottom(self, max_tries=4):
        """把聊天记录滚到最新一条。每次滚一大段，等画面稳定后再判断：
        这一滚如果画面没变，说明已经到底了。
        原实现每轮死等 1.5 秒、最多 8 轮，一条回复光这里就要十几秒。"""
        h = self.chat_hash()
        for _ in range(max_tries):
            self._ensure_front()
            x, y = self._abs(self.lay.chat_x + self.lay.chat_w / 2, (self.lay.title_h + self.lay.input_top) / 2)
            macos.move(x, y)
            time.sleep(0.05)
            for _ in range(20):
                macos.scroll_px(-120)
                time.sleep(0.01)
            h2 = self._wait_chat_stable()
            if h2 == h:
                return True
            h = h2
        return False

    def _wait_stable(self, timeout=1.0):
        """等列表滚动动画停下来，返回停下后的画面指纹。"""
        h = self.list_hash()
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.15)
            h2 = self.list_hash()
            if h2 == h:
                return h
            h = h2
        return h

    def _wait_chat_stable(self, timeout=1.0):
        """等聊天区画面连续两次采样一致，返回稳定后的指纹。"""
        prev = None
        end = time.time() + timeout
        while time.time() < end:
            cur = self.chat_hash()
            if cur == prev:
                return cur
            prev = cur
            time.sleep(0.1)
        return prev

    def _scroll_px(self, px):
        self._ensure_front()
        x, y = self._abs(self.lay.list_x + self.lay.list_w / 2, (self.lay.list_top + self.lay.win_h) / 2)
        macos.move(x, y)
        time.sleep(0.05)
        step = 120 if px > 0 else -120
        for _ in range(max(1, int(abs(px) // 120))):
            macos.scroll_px(step)
            time.sleep(0.01)

    def scroll_list_to_top(self, max_tries=30):
        """一直往上滚，直到画面不再变化（= 到顶）。返回是否确认到顶。"""
        h = self.list_hash()
        for _ in range(max_tries):
            self._scroll_px(3000)
            h2 = self._wait_stable()
            if h2 == h:
                return True
            h = h2
        return False

    def scroll_list_page(self):
        """往下翻约 3/4 屏（相邻两屏有重叠，不会漏行）。返回 False 表示画面没变 = 已到底。"""
        before = self._wait_stable()
        self._scroll_px(-(self.lay.win_h - self.lay.list_top) * 0.75)
        return self._wait_stable() != before

    def open_row(self, row):
        self._ensure_front()
        x, y = self._abs(self.lay.list_x + self.lay.list_w * 0.6, row["y"] + row.get("h", 16) / 2 + 6)
        macos.click(x, y)
        time.sleep(self.settle)

    def quote(self, text, snap=None):
        """右键那条消息 → 点菜单里的「引用」，输入框上方会出现引用块。成功返回 True。
        找不到那条消息、或菜单没点中，就返回 False，调用方退回普通发送。
        菜单是独立的弹出窗口，按微信主窗口截图看不到它，只截鼠标附近一小块做 OCR。
        snap 可以传调用方刚截好的画面，省一次整窗截图+OCR。"""
        snap = snap or self.snapshot()
        if snap is None:
            return False
        msgs = [m for m in parse_chat(snap[2], self.lay)["messages"] if not m["me"]]
        key = re.sub(r"\s", "", text or "")[:10]
        cand = [m for m in msgs if key and re.sub(r"\s", "", m["text"]).startswith(key)]
        if not cand:
            return False
        m = cand[-1]
        self._ensure_front()
        # 点在气泡里的文字上（短消息的气泡很窄，不能固定 x）
        sx, sy = self._abs(m.get("x", self.lay.chat_x + 140) + min(20, max(6, m.get("w", 40) / 2)),
                           m["y"] + 7)
        macos.right_click(sx, sy)
        time.sleep(0.6)
        # 菜单就在鼠标附近，只截 900×900 的区域，比整屏 OCR 快好几倍
        rect = macos.screen_rect_around(sx, sy, 900, 900)

        def find(lines):
            return next((l for l in lines
                         if "引用" in l.text.strip()
                         and abs(rect.x + l.cx - sx) < 420 and abs(rect.y + l.cy - sy) < 440), None)

        img, scale = macos.screenshot(rect)
        lines = macos.ocr(img, scale, fast=True)
        item = find(lines)
        if item is None:  # 快速模式偶尔认不出，用精确模式兜底
            lines = macos.ocr(img, scale)
            item = find(lines)
        if item is None:  # 菜单可能还没弹出来，再等一下重截一次
            time.sleep(0.25)
            img, scale = macos.screenshot(rect)
            lines = macos.ocr(img, scale, fast=True)
            item = find(lines)
        if item is None:
            log.debug("右键菜单里没找到「引用」，OCR 到：%s", [l.text for l in lines][:15])
            macos.key(macos.KEY_ESC)
            return False
        macos.click(rect.x + item.cx, rect.y + item.cy)
        time.sleep(0.25)
        # 引用块在输入框上方，只截这一条做 OCR，别再把整个窗口识别一遍
        img, scale = macos.screenshot(self.win)  # 按窗口截，被别的窗口挡住也不影响
        crop = img.crop((int(self.lay.chat_x * scale), int((self.lay.input_top - 110) * scale),
                         int((self.lay.chat_x + self.lay.chat_w) * scale), int(self.lay.input_top * scale)))
        # 形如「发送者：消息开头」。OCR 对 @、0/o 之类容易认错，用片段重合判断
        want = re.sub(r"\s", "", text or "")
        for l in macos.ocr(crop, scale, fast=True):
            t = re.sub(r"\s", "", l.text)
            if "：" in t and sum(1 for i in range(len(want) - 2) if want[i:i + 3] in t) >= 2:
                return True
        return False

    def send(self, text):
        """点输入框 → 粘贴 → 回车。发完恢复剪贴板。"""
        # 偏下一点：上面可能是引用块，点到引用块会把引用取消掉
        x, y = self._abs(self.lay.chat_x + self.lay.chat_w / 2,
                         self.lay.input_top + (self.lay.win_h - self.lay.input_top) * 0.6)
        self._ensure_front()
        old = macos.clipboard_get()
        try:
            macos.click(x, y)
            time.sleep(0.2)
            macos.clipboard_set(text)
            time.sleep(0.1)
            macos.key(macos.KEY_V, cmd=True)
            time.sleep(0.3)
            macos.key(macos.KEY_RETURN)
            time.sleep(0.4)
        finally:
            macos.clipboard_set(old)
