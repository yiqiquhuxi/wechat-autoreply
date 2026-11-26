"""后台主流程：看角标 → 扫红点 → 打开会话 → 识别 → 生成回复 → 确认/发送。"""
import collections
import datetime as dt
import difflib
import hashlib
import json
import logging
import random
import re
import subprocess
import threading
import time

from . import llm, macos
from .memory import ChatMemory
from .wechat import WeChatUI, attach_badges, find_avatars, find_badges, parse_chat, parse_session_rows

log = logging.getLogger("wxbot")
SKIP_NAMES = ("服务号", "订阅号", "公众号", "文件传输助手", "微信团队", "微信支付", "腾讯新闻")
STATE_PATH = "state.json"
FORCE_CHECK_SECONDS = 30  # 必回群：没有红点也每隔这么久去看一眼，确认最后一条是不是我发的
WHITELIST_CHECK_SECONDS = 90  # 其他白名单会话：红点不一定是唯一信号（免打扰没有红点），定期也看一眼


def norm(s):
    """去掉群人数后缀、空格、表情和标点，用于名称比对。"""
    s = re.sub(r"[（(]\s*\d+\s*[)）]\s*$", "", s or "")
    return re.sub(r"[^\w一-鿿]", "", s).lower()


def match_name(ocr_name, names, threshold):
    """OCR 出来的名称和白名单做匹配：先精确，再模糊。返回白名单里的原名或 None。"""
    n = norm(ocr_name)
    if not n:
        return None
    for name in names:
        if norm(name) == n:
            return name
    best, score = None, 0.0
    for name in names:
        r = difflib.SequenceMatcher(None, norm(name), n).ratio()
        if r > score:
            best, score = name, r
    return best if score >= threshold else None


_OCR_FIX = str.maketrans({"0": "o", "1": "l", "|": "l", "5": "s", "8": "b"})

MEDIA_KINDS = ("图片", "视频", "语音", "文件", "动画表情", "表情", "链接", "位置", "名片",
               "小程序", "视频号", "红包", "转账", "音乐", "聊天记录")

_LOG_LINE = re.compile(
    r"(打开的会话标题|停止本轮操作|暂不回复：|最新一条是我发的|这些消息已处理过|"
    r"当前屏 未读|当前屏 主动检查|微信不在最前面|没引用成功，直接发|已引用对方那条消息|"
    r"耗时：打开\+识别)")


def looks_like_log(text):
    """用户把 bot 运行日志贴进聊天时，别把日志当成对话内容喂给模型。"""
    return bool(_LOG_LINE.search(text or ""))

_SENDER_OK = re.compile(r"^[\w\u4e00-\u9fff·\-.()（） ]{1,24}$")


def clean_sender(s):
    """OCR 出来的群昵称经常带杂讯，看起来不像名字就当没有。"""
    s = (s or "").strip().strip(":：")
    return s if _SENDER_OK.match(s) else None


_MEDIA_TOKEN = re.compile(r"[\[［]([^\[\]［］]{1,12})[\]］]")


def media_kind(text):
    """会话预览里是不是有「［图片］」这类媒体占位符；有就返回类型名。
    群聊预览常带发送者名字（如「张三: [图片]」），所以按 [] 里的词单独判断。"""
    for tok in _MEDIA_TOKEN.findall(text or ""):
        tok = re.sub(r"\s", "", tok).replace("有人@我", "")
        if tok in MEDIA_KINDS:
            return tok
    return None


def mentions(text, nick):
    """消息里有没有 @我。OCR 常把 o 认成 0、l 认成 1，所以做容错的模糊比对。"""
    me = norm(nick).translate(_OCR_FIX)
    if not me:
        return False
    for part in text.split("@")[1:]:
        cand = norm(part[: len(nick) + 4]).translate(_OCR_FIX)
        if cand.startswith(me) or difflib.SequenceMatcher(None, me, cand[: len(me)]).ratio() >= 0.8:
            return True
    return False


def in_quiet_hours(cfg):
    q = cfg.get("quiet_hours")
    if not q or len(q) != 2 or not q[0] or not q[1]:
        return False
    now = dt.datetime.now().strftime("%H:%M")
    start, end = q
    return (start <= now < end) if start < end else (now >= start or now < end)


def confirm(chat, incoming, draft, timeout):
    """系统弹窗：可以修改回复内容，点「发送」才发。超时或点「跳过」不发。"""
    script = [
        "on run argv",
        "set r to display dialog (item 1 of argv) default answer (item 2 of argv) "
        f'with title "微信自动回复" buttons {{"跳过", "发送"}} default button "发送" giving up after {int(timeout)}',
        'if gave up of r then return ""',
        'if button returned of r is "发送" then return "OK" & linefeed & (text returned of r)',
        'return ""',
        "end run",
    ]
    prompt = f"【{chat}】\n{incoming[-300:]}\n\n回复（可修改）："
    args = ["osascript"] + sum((["-e", s] for s in script), []) + [prompt, draft]
    out = subprocess.run(args, capture_output=True, text=True).stdout.rstrip("\n")
    if not out.startswith("OK\n"):
        return None
    return out[3:].strip() or None


class Status:
    """给界面看的运行状态。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.text = "未启动"
        self.logs = collections.deque(maxlen=300)
        self.sent_today = 0
        self.day = dt.date.today()

    def set(self, text):
        with self.lock:
            self.text = text

    def count_sent(self):
        with self.lock:
            if dt.date.today() != self.day:
                self.day, self.sent_today = dt.date.today(), 0
            self.sent_today += 1

    def snapshot(self):
        with self.lock:
            return {"text": self.text, "sent_today": self.sent_today, "logs": list(self.logs)}


class StatusLogHandler(logging.Handler):
    def __init__(self, status):
        super().__init__(logging.INFO)
        self.status = status

    def emit(self, record):
        line = f"{dt.datetime.fromtimestamp(record.created):%H:%M:%S} {record.getMessage()}"
        with self.status.lock:
            self.status.logs.append(line)


class Bot:
    def __init__(self, store, status=None, dry_run=False):
        self.store = store
        self.status = status or Status()
        self.dry = dry_run
        self.ui = WeChatUI()
        self.sent = collections.deque()
        self.last_by_chat = {}
        self.stop_event = threading.Event()
        self.pending = {}  # 点开过、但这次没处理完的会话（红点已经没了），下轮按名字回来找
        self.memory = ChatMemory(persist=not dry_run)  # 每个会话的聊天记忆，屏幕外的上下文靠它
        self.force_checked = {}  # 必回群上次检查的时间
        self._scan_bottom = False  # 上一次 scan_unread 是否已经翻到列表底部
        macos.should_stop = lambda: self.stop_event.is_set() or (not self.dry and not self.store.get()["enabled"])
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                self.state = json.load(f)
        except FileNotFoundError:
            self.state = {}

    # ----- 小工具 -----

    def _all_names(self, cfg):
        wl = cfg["whitelist"]
        return wl.get("private", []) + wl.get("groups", [])

    def _always_names(self, cfg):
        return cfg.get("always_reply") or []

    def _interval(self, cfg, target):
        return FORCE_CHECK_SECONDS if target in self._always_names(cfg) else WHITELIST_CHECK_SECONDS

    def _check_due(self, cfg, target):
        return time.time() - self.force_checked.get(target, 0) >= self._interval(cfg, target)

    def _any_due(self, cfg):
        return any(self._check_due(cfg, t) for t in self._all_names(cfg) + self._always_names(cfg))

    def _should_scan_more(self, cfg, total):
        """当前屏处理完后，还值不值得滚列表继续找。
        没未读、没可处理的待办、也没有到点会话时，直接结束本轮，省掉一次全列表扫描。"""
        if total is None or total > 0:
            return True
        if any(self.allow(cfg, t)[0] for t in self.pending):
            return True
        return self._any_due(cfg)

    def classify(self, cfg, name):
        """私聊白名单、群白名单、必回群是三个互不相干的名单，各自匹配。
        返回 (名单里的原名, 类别) 或 None；类别是 private / group / always。"""
        if any(k in (name or "") for k in SKIP_NAMES):
            return None
        th = cfg["match_threshold"]
        wl = cfg["whitelist"]
        for kind, names in (("always", self._always_names(cfg)),
                            ("private", wl.get("private", [])),
                            ("group", wl.get("groups", []))):
            m = match_name(name, names, th)
            if m:
                return m, kind
        return None

    def whitelisted(self, cfg, name):
        hit = self.classify(cfg, name)
        return hit[0] if hit else None

    def allow(self, cfg, chat):
        lim = cfg["limits"]
        now = time.time()
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        if len(self.sent) >= lim["max_replies_per_hour"]:
            return False, f"本小时已发 {len(self.sent)} 条，达到上限"
        last = self.last_by_chat.get(chat)
        if last and now - last < lim["per_chat_cooldown_seconds"]:
            return False, f"{int(lim['per_chat_cooldown_seconds'] - (now - last))} 秒内刚回过这个会话"
        return True, ""

    def _rate_limited(self, cfg):
        """本小时回复数到上限时，连列表都不用扫，更不要点开会话标已读。"""
        lim = cfg["limits"]
        now = time.time()
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        return len(self.sent) >= lim["max_replies_per_hour"]

    def _defer(self, target, row, why):
        """这次先不回，但会话已经点开、红点没了；记下来下轮再处理。"""
        mentioned = row is not None and "有人@我" in (row.get("preview") or "")
        old = self.pending.get(target, {})
        self.pending[target] = {"since": old.get("since", time.time()), "mentioned": old.get("mentioned") or mentioned}
        log.info("[%s] 暂不回复：%s（稍后再来）", target, why)

    def _mark(self, target, key):
        self.pending.pop(target, None)
        self.state[target] = {"last_key": key, "at": dt.datetime.now().isoformat(timespec="seconds")}
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    # ----- 流程 -----

    def _hits(self, cfg, snap, where):
        """在这一屏里找「白名单 + 有红点」的会话。返回 ((row, 白名单名) 或 None, 这屏红点数)。"""
        img, scale, lines = snap
        rows = parse_session_rows(lines, self.ui.lay, find_avatars(img, scale, self.ui.lay))
        hits = attach_badges(rows, find_badges(img, scale, self.ui.lay))
        for h in hits:
            target = self.whitelisted(cfg, h["name"])
            log.debug("%s 未读: %s -> %s", where, h["name"], target or "不在白名单")
            if target:
                return (h, target), len(hits)
        # 没有红点的会话也要看（免打扰的根本没有红点）：挑这一屏里最久没检查过的那个，
        # 别让 30 秒一查的必回群把 90 秒一查的私聊/群白名单挤掉
        cands = []
        for r in rows:
            t = self.whitelisted(cfg, r["name"])
            if t and self._check_due(cfg, t):
                cands.append((self.force_checked.get(t, 0), r, t))
        if cands:
            _, r, t = min(cands, key=lambda c: c[0])
            self.force_checked[t] = time.time()
            log.debug("%s 主动检查: %s（无红点也看）", where, t)
            return (r, t), len(hits)
        # 之前点开过但没处理完的会话，红点已经没了，按名字找
        now = time.time()
        for t, info in list(self.pending.items()):
            if now - info["since"] > 1800:  # 半小时还没处理掉就放弃
                self.pending.pop(t, None)
                continue
            ok, _ = self.allow(cfg, t)
            if not ok:
                continue
            for r in rows:
                if match_name(r["name"], [t], cfg["match_threshold"]):
                    return (r, t), len(hits)
        return None, len(hits)

    def _find_row(self, snap, target, th):
        """在这一屏的会话列表里按名字找一行；找不到返回 None。"""
        img, scale, lines = snap
        rows = parse_session_rows(lines, self.ui.lay, find_avatars(img, scale, self.ui.lay))
        for r in rows:
            if match_name(r["name"], [target], th):
                return r
        return None

    def _screen_tasks(self, cfg, snap, max_tasks):
        """只看当前这一屏，收集能马上处理的会话，带着行坐标返回。
        先处理眼前的任务，当前屏没有了才翻页——避免为了凑批次先扫全列表，把第一条回复拖慢。"""
        img, scale, lines = snap
        rows = parse_session_rows(lines, self.ui.lay, find_avatars(img, scale, self.ui.lay))
        hits = attach_badges(rows, find_badges(img, scale, self.ui.lay))
        th = cfg["match_threshold"]
        now = time.time()
        tasks, seen = [], set()

        # 1) 红点未读
        for h in hits:
            target = self.whitelisted(cfg, h["name"])
            log.debug("当前屏 未读: %s -> %s", h["name"], target or "不在白名单")
            if not target or target in seen or len(tasks) >= max_tasks:
                continue
            ok, why = self.allow(cfg, target)
            if not ok:
                # 冷却/限流中先别点开：点开会把消息标已读，还白占一个本轮名额
                old = self.pending.get(target, {})
                self.pending[target] = {
                    "since": old.get("since", now),
                    "mentioned": old.get("mentioned") or ("有人@我" in (h.get("preview") or "")),
                }
                seen.add(target)
                log.debug("当前屏 冷却中先不打开: %s（%s）", target, why)
                continue
            tasks.append((target, h, "未读"))
            seen.add(target)
            self.force_checked[target] = now

        # 2) 之前点开过、但没处理完的（红点已经没了），当前屏看得到就排上
        for t, info in list(self.pending.items()):
            if now - info["since"] > 1800:  # 半小时还没处理掉就放弃
                self.pending.pop(t, None)
                continue
            if t in seen or len(tasks) >= max_tasks:
                continue
            ok, _ = self.allow(cfg, t)
            if not ok:
                continue
            row = next((r for r in rows if match_name(r["name"], [t], th)), None)
            if row:
                tasks.append((t, row, "待处理"))
                seen.add(t)
                self.force_checked[t] = now

        # 3) 没有红点、但到点该看的（免打扰群根本没有红点）
        cands = []
        for r in rows:
            t = self.whitelisted(cfg, r["name"])
            if not t or t in seen or not self._check_due(cfg, t):
                continue
            ok, why = self.allow(cfg, t)
            if not ok:
                # 冷却/限流中先不打开，免得白标已读；force_checked 不动，等能回的时候再看
                seen.add(t)
                log.debug("当前屏 冷却中先不打开: %s（%s）", t, why)
                continue
            cands.append((self.force_checked.get(t, 0), t, r))
        for _, t, r in sorted(cands, key=lambda c: (c[0], c[1])):
            if len(tasks) >= max_tasks:
                break
            self.force_checked[t] = time.time()
            tasks.append((t, r, "主动检查"))
            seen.add(t)

        rank = {"未读": 0, "待处理": 1, "主动检查": 2}
        tasks.sort(key=lambda x: rank[x[2]])
        return tasks, seen

    def scan_unread(self, cfg, expected_total):
        """找第一个命中白名单、带红点的会话，返回 (row, 白名单名)；找不到返回 None。
        先看当前这一屏；没有再滚到顶，然后每次往下翻 3/4 屏，直到到底或翻够页数。"""
        self._scan_bottom = False
        snap = self.ui.snapshot()
        if snap is None:
            log.warning("找不到微信主窗口（最小化了，或在别的桌面？），这轮跳过")
            return None
        found, _ = self._hits(cfg, snap, "当前屏")
        if found:
            return found
        if not self.ui.scroll_list_to_top():
            log.warning("滚了很多次还没到顶，按当前位置继续往下找")
        seen = 0
        for page in range(cfg["max_scroll_pages"]):
            snap = self.ui.snapshot()
            if snap is None:
                return None
            found, n = self._hits(cfg, snap, f"第 {page + 1} 屏")
            if found:
                return found
            seen += n
            if expected_total and seen >= expected_total and not self._any_due(cfg):
                break
            if not self.ui.scroll_list_page():
                log.debug("列表到底了")
                self._scan_bottom = True
                break
        return None

    def handle(self, cfg, target, row=None):
        th = cfg["match_threshold"]
        t_start = time.time()
        if row is not None and not self.dry:
            self.ui.open_row(row)
            self.ui.scroll_chat_to_bottom()  # 会话可能停在上次看到的位置，滚到最新
        snap = self.ui.snapshot()
        if snap is None:
            return
        chat = parse_chat(snap[2], self.ui.lay)
        if row is not None and not self.dry and match_name(chat["title"], [target], th) is None:
            # 多半只是画面还没渲染完：先等一下再识别一次，比整套重开省 5~8 秒
            log.debug("第一次点击没打开「%s」（标题「%s」），等一下再识别", target, chat["title"])
            time.sleep(0.4)
            snap = self.ui.snapshot()
            if snap is not None:
                chat = parse_chat(snap[2], self.ui.lay)
            if match_name(chat["title"], [target], th) is None:
                log.debug("还是没打开「%s」，重新点一次", target)
                self.ui.open_row(row)  # 偶尔点击会被切窗口吃掉，再点一次
                self.ui.scroll_chat_to_bottom()
                snap = self.ui.snapshot()
                if snap is None:
                    return
                chat = parse_chat(snap[2], self.ui.lay)
        # 安全校验：打开后的标题必须还是白名单里的这个人/群，否则放弃
        if match_name(chat["title"], [target], th) is None:
            log.warning("打开的会话标题「%s」和目标「%s」对不上，放弃", chat["title"], target)
            return
        is_group = target in cfg["whitelist"].get("groups", []) or chat["is_group"]
        # 私聊名单和群名单互相独立：打开后按会话类型再核对一次，串了就放弃
        kind = ("always" if target in self._always_names(cfg)
                else "private" if target in cfg["whitelist"].get("private", []) else "group")
        if kind in ("private", "group") and (kind == "private") == is_group:
            log.warning("「%s」在%s名单里，但打开的是%s，放弃", target,
                        "私聊" if kind == "private" else "群", "群聊" if is_group else "私聊")
            return

        msgs = [m for m in chat["messages"] if not looks_like_log(m["text"])]
        if len(msgs) < len(chat["messages"]):
            log.debug("[%s] 忽略 %d 条像运行日志的消息", target, len(chat["messages"]) - len(msgs))
        last_me = max((i for i, m in enumerate(msgs) if m["me"]), default=-1)
        new_in = [m for m in msgs[last_me + 1:] if not m["me"]]
        media = None
        if not new_in:
            # 图片/表情/语音这类消息屏幕上没有文字，OCR 什么也读不到，
            # 但会话预览是微信自己写的「［图片］」，说明对方确实发了新消息
            media = media_kind((row or {}).get("preview")) or media_kind(msgs[-1]["text"] if msgs else "")
            if not media:
                log.debug("[%s] 最新一条是我发的，跳过（预览=%r）", target, (row or {}).get("preview"))
                self.pending.pop(target, None)  # 已经没什么要回的了，别再占着待处理名额
                return
            log.info("[%s] 对方发的是%s（屏幕上没字，按媒体消息处理）", target, media)
            new_in = [{"text": f"［{media}］", "me": False, "sender": None}]
        key = hashlib.sha1("\n".join(m["text"] for m in new_in).encode()).hexdigest()
        if media and (row or {}).get("time"):
            key = hashlib.sha1(f"{key}:{row['time']}".encode()).hexdigest()  # 同一条媒体消息反复扫到要能认出来
        if self.state.get(target, {}).get("last_key") == key:
            log.info("[%s] 这些消息已处理过，跳过", target)
            self.pending.pop(target, None)
            return

        always = self._always_names(cfg)
        force = target in always  # 这些会话：最后一条不是我发的一定要回，模型也不许跳过
        if is_group and cfg["group_reply_only_when_mentioned"] and not force:
            # 会话预览里的「[有人@我]」是微信自己的标记，最可靠；群昵称和设置里填的不一致时也能认出来
            flagged = (row is not None and "有人@我" in (row.get("preview") or "")) or self.pending.get(target, {}).get("mentioned")
            by_text = any(mentions(m["text"], cfg["my_nickname"]) for m in new_in)
            if not (flagged or by_text):
                log.info("[%s] 群里没人 @我，跳过", target)
                self._mark(target, key)
                return

        ok, why = self.allow(cfg, target)
        if not ok:
            self._defer(target, row, why)
            return

        t_read = time.time()
        self.status.set(f"正在为「{target}」生成回复")
        self.memory.merge(target, msgs)
        n_ctx = max(1, int(cfg["context_messages"]))
        # 多取一些再过滤，免得日志把真正的上下文挤掉
        ctx = [m for m in self.memory.tail(target, n_ctx * 2) if not looks_like_log(m["text"])][-n_ctx:]
        if not ctx:
            ctx = msgs[-n_ctx:]
        if media:  # 媒体消息屏幕上读不到，补进上下文，不然模型看到的最后一条还是我发的
            ctx = ctx + new_in
        draft = llm.generate(cfg["llm"], target, is_group, ctx, cfg["my_nickname"],
                             must_reply=force, new_messages=new_in)
        if not draft and force:
            draft = random.choice(["嗯嗯", "收到", "好嘞", "行"])  # 模型还是不想回，就补一句，保证我的消息是最新的
        if is_group and cfg.get("reply_mention_back", True) and draft:
            who = next((clean_sender(m.get("sender")) for m in reversed(new_in) if clean_sender(m.get("sender"))), None)
            if who and not draft.startswith("@" + who):  # 群里回谁就 @ 谁，免得看不出在跟谁说话
                draft = f"@{who} {draft}"
        t_gen = time.time()
        incoming = "\n".join(m["text"] for m in new_in)
        if not draft:
            log.info("[%s] 模型判断不需要回复", target)
            self._mark(target, key)
            return
        log.info("[%s] 收到：%s ｜ 草稿：%s", target, incoming.replace("\n", " / "), draft)
        if self.dry:
            return

        if cfg["mode"] == "confirm":
            self.status.set(f"等你确认「{target}」的回复")
            text = confirm(target, incoming, draft, cfg["confirm_timeout_seconds"])
            if not text:
                log.info("[%s] 你选择了不发", target)
                self._mark(target, key)
                return
            if not macos.activate_wechat():
                log.warning("[%s] 没能把微信切到前台，没发送", target)
                return
            time.sleep(0.3)
            macos.begin_guard()
            snap = self.ui.snapshot()
            if snap is None or match_name(parse_chat(snap[2], self.ui.lay)["title"], [target], th) is None:
                log.warning("[%s] 当前会话已变化，放弃发送", target)
                return
        else:
            text = draft
            delay = random.uniform(*cfg["limits"]["reply_delay_seconds"])
            self.status.set(f"{delay:.0f} 秒后回复「{target}」")
            if self.stop_event.wait(delay):
                return
            if macos.user_idle_seconds() < delay - 1:
                log.info("[%s] 等待期间你动了电脑，这次不发", target)
                return
            if not macos.activate_wechat():
                log.warning("[%s] 没能把微信切到前台，没发送", target)
                return
            time.sleep(0.3)
            macos.begin_guard()
            snap = self.ui.snapshot()
            if snap is None or match_name(parse_chat(snap[2], self.ui.lay)["title"], [target], th) is None:
                log.warning("[%s] 当前会话已变化，放弃发送", target)
                return

        if not self.store.get()["enabled"]:  # 等待期间被关掉了
            log.info("[%s] 已暂停，不发送", target)
            return
        t_send = time.time()
        if cfg.get("quote_reply", True) and not media:
            src = next((m["text"] for m in reversed(new_in) if not m["me"] and m["text"].strip()), "")
            if src and self.ui.quote(src, snap):
                log.debug("[%s] 已引用对方那条消息", target)
            elif src:
                log.debug("[%s] 没引用成功，直接发", target)
        log.debug("[%s] 耗时：打开+识别 %.1fs，生成 %.1fs，引用 %.1fs",
                  target, t_read - t_start, t_gen - t_read, time.time() - t_send)
        self.ui.send(text)
        self.memory.add(target, text, me=True)  # 自己发的也记进上下文
        self.sent.append(time.time())
        self.last_by_chat[target] = time.time()
        self.status.count_sent()
        self._mark(target, key)
        log.info("[%s] 已发送：%s", target, text)

    @staticmethod
    def _restore(front, mouse):
        """把前台窗口和鼠标位置原样还回去。"""
        if macos.user_interrupted():
            return
        if front is not None and str(front.bundleIdentifier() or "") != macos.WECHAT_BUNDLE_ID:
            macos.activate(front)
        macos.move(*mouse, guard=False)

    def tick(self, cfg):
        self.ui.overrides = cfg.get("layout_overrides") or {}
        if in_quiet_hours(cfg):
            self.status.set("免打扰时段，暂停中")
            return
        total = macos.dock_badge()
        if total == 0 and not self.pending and not self._any_due(cfg):
            self.status.set("运行中 · 没有未读")
            return
        if not self.dry and macos.user_idle_seconds() < cfg["idle_seconds_required"]:
            self.status.set(f"运行中 · 有 {total} 条未读，等你离开电脑")
            return
        if not self.dry and self._rate_limited(cfg):
            self.status.set("运行中 · 本小时回复已达上限")
            return

        self.status.set(f"运行中 · 处理 {total if total is not None else '?'} 条未读")
        front = macos.frontmost_app()
        mouse = macos.mouse_pos()
        interrupted = False
        try:
            if not macos.activate_wechat():
                log.warning("没能把微信切到前台（窗口最小化了，或在别的桌面？），这轮跳过")
                return
            time.sleep(0.3)
            macos.begin_guard()
            if self.dry:
                found = self.scan_unread(cfg, total)
                log.info("白名单里第一个有未读的会话：%s", found[1] if found else "无")
                snap = self.ui.snapshot()
                if snap:
                    title = parse_chat(snap[2], self.ui.lay)["title"]
                    target = self.whitelisted(cfg, title)
                    log.info("当前打开的会话：%s（%s）", title or "未识别", "在白名单" if target else "不在白名单，不生成草稿")
                    if target:
                        self.handle(cfg, target)
                return
            max_tasks = max(1, int(cfg["max_chats_per_round"]))
            done = 0
            tried = set()

            # 先处理当前屏：只扫一次，能看到的任务立刻连续处理。
            # 这一步不做任何翻页，所以第一条回复不会被全列表扫描拖慢。
            snap = self.ui.snapshot()
            screen_tasks, screen_seen = (
                self._screen_tasks(cfg, snap, max_tasks) if snap else ([], set()))
            if screen_tasks:
                log.info("当前屏一次找到 %d 个待处理会话：%s", len(screen_tasks),
                         "、".join(f"{t}（{why}）" for t, _, why in screen_tasks))
                for i, (target, row, _why) in enumerate(screen_tasks):
                    if not self.store.get()["enabled"] or self._rate_limited(cfg):
                        break
                    if i > 0:
                        # 发完上一条后会话会跳到列表顶部，旧坐标会点错人；
                        # 只在当前屏按名字重新找，找不到就留到下一轮，绝不为它翻列表。
                        time.sleep(0.1)
                        if not macos.activate_wechat():
                            break
                        snap2 = self.ui.snapshot()
                        row = self._find_row(snap2, target, cfg["match_threshold"]) if snap2 else None
                        if row is None:
                            log.debug("当前屏找不到「%s」了，留到下一轮", target)
                            continue
                    try:
                        self.handle(cfg, target, row)
                        done += 1
                        tried.add(target)
                    except macos.UserBusy as e:
                        self._defer(target, row, str(e))
                        raise
                    except Exception as e:
                        self._defer(target, row, f"出错：{e}")
                        raise

            # 当前屏名额被占满时，给「没扫到、但已经到点」的会话额外留一个补查名额。
            # 否则当前屏的必回群每轮都占满 5 个，列表下面的胡俊峰永远轮不到。
            all_names = self._all_names(cfg) + self._always_names(cfg)
            unseen_due = [t for t in all_names
                          if t not in screen_seen and t not in tried
                          and self._check_due(cfg, t) and self.allow(cfg, t)[0]]
            extra = bool(unseen_due) and done >= max_tasks
            if extra:
                log.info("当前屏名额已满，额外补查一个没扫到的到点会话：%s", "、".join(unseen_due))

            # 当前屏处理完还没到本轮上限，再按原来的方式滚到顶往下翻。
            # 眼前的任务已经先回完了，这里只影响下面会话的发现速度。
            while done < max_tasks or extra:
                extra = False  # 只额外补一个，别把一轮拖太长
                if not self.store.get()["enabled"] or self._rate_limited(cfg):
                    break
                cur_total = macos.dock_badge()
                if not self._should_scan_more(cfg, cur_total):
                    break
                if not macos.activate_wechat():
                    break
                time.sleep(0.15)
                found = self.scan_unread(cfg, cur_total)
                if not found:
                    if self._scan_bottom:
                        # 翻到底都没有，说明这些会话不在列表里（归档/改名/退群），
                        # 先标记成已检查，免得每一轮都翻到底找它们。
                        now = time.time()
                        for t in all_names:
                            if t not in screen_seen and self._check_due(cfg, t):
                                self.force_checked[t] = now
                    break
                row, target = found
                if target in tried:
                    log.debug("「%s」本轮已经试过，先跳过", target)
                    break
                try:
                    self.handle(cfg, target, row)
                    done += 1
                    tried.add(target)
                except macos.UserBusy as e:
                    self._defer(target, row, str(e))
                    raise
                except Exception as e:
                    self._defer(target, row, f"出错：{e}")
                    raise
        except macos.UserBusy as e:
            interrupted = True
            log.info("停止本轮操作：%s", e)
        finally:
            # 被你打断时不挪鼠标、不切窗口，免得跟你抢
            if not interrupted:
                self._restore(front, mouse)

    def loop(self):
        while not self.stop_event.is_set():
            cfg = self.store.get()
            if not cfg["enabled"] and not self.dry:
                self.status.set("已暂停")
                self.stop_event.wait(1)
                continue
            try:
                self.tick(cfg)
            except Exception as e:
                log.exception("本轮出错：%s", e)
                self.status.set(f"出错：{e}")
            if self.dry:
                return
            # 分段等待，便于界面上开关后尽快生效
            for _ in range(int(cfg["poll_seconds"])):
                if self.stop_event.is_set() or self.store.get()["enabled"] != cfg["enabled"]:
                    break
                time.sleep(1)

    def stop(self):
        self.stop_event.set()
        macos.ABORT.set()
