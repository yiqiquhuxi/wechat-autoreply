"""每个会话的聊天记忆：屏幕只显示最近一小段，这里把见过的消息攒起来，
让模型回复时能接上之前的上下文。只存在本机 history.json。"""
import datetime as dt
import json
import logging
import os
import threading

log = logging.getLogger("wxbot")
PATH = "history.json"


class ChatMemory:
    MAX_PER_CHAT = 200  # 每个会话最多留这么多条，太老的丢掉

    def __init__(self, path=PATH, persist=True):
        self.path = path
        self.persist = persist
        self.lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (FileNotFoundError, ValueError):
            self.data = {}

    # ----- 写入 -----

    def merge(self, chat, msgs):
        """把屏幕上读到的消息并进历史。屏幕上通常有和历史重叠的一段，从重叠处往后追加。"""
        with self.lock:
            h = self.data.setdefault(chat, [])
            start = 0
            if h:
                tail = (h[-1]["me"], h[-1]["text"])
                pos = [i for i, m in enumerate(msgs) if (bool(m["me"]), m["text"]) == tail]
                if pos:
                    k = pos[-1]
                    n = min(k + 1, len(h))
                    if ([(bool(m["me"]), m["text"]) for m in msgs[k - n + 1 : k + 1]]
                            == [(x["me"], x["text"]) for x in h[-n:]]):
                        start = k + 1
            now = dt.datetime.now().isoformat(timespec="seconds")
            for m in msgs[start:]:
                h.append({"me": bool(m["me"]), "sender": m.get("sender"), "text": m["text"], "at": now})
            if len(h) > self.MAX_PER_CHAT:
                del h[: -self.MAX_PER_CHAT]
            self._save()

    def add(self, chat, text, me=True, sender=None):
        """把自己发出去的回复也记下来（下次屏幕上看得到，但先记着更保险）。"""
        with self.lock:
            h = self.data.setdefault(chat, [])
            h.append({"me": bool(me), "sender": sender, "text": text,
                      "at": dt.datetime.now().isoformat(timespec="seconds")})
            if len(h) > self.MAX_PER_CHAT:
                del h[: -self.MAX_PER_CHAT]
            self._save()

    # ----- 读取 -----

    def tail(self, chat, n):
        with self.lock:
            return [dict(m) for m in self.data.get(chat, [])[-n:]] if n > 0 else []

    # ----- 落盘 -----

    def _save(self):
        if not self.persist:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as e:  # 记忆存不下来不该影响回复
            log.warning("聊天记忆保存失败：%s", e)
