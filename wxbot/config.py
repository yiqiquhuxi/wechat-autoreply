"""配置的默认值、读写（线程安全），界面和后台共用。"""
import copy
import json
import os
import threading

PATH = "config.json"

# 模型预设：只预填接口地址，模型名按各家文档填写
PROVIDER_PRESETS = {
    "claude": {"provider": "anthropic", "base_url": "", "model": "claude-opus-5-5"},
    "deepseek": {"provider": "openai_compatible", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "qwen": {"provider": "openai_compatible", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": ""},
    "kimi": {"provider": "openai_compatible", "base_url": "https://api.moonshot.cn/v1", "model": ""},
    "ollama": {"provider": "openai_compatible", "base_url": "http://localhost:11434/v1", "model": ""},
    "custom": {"provider": "openai_compatible", "base_url": "", "model": ""},
}

DEFAULTS = {
    "enabled": False,
    "mode": "confirm",
    "my_nickname": "",
    "whitelist": {"private": [], "groups": []},
    "group_reply_only_when_mentioned": True,
    "reply_mention_back": True,  # 群里回复时自动 @ 对方
    "quote_reply": True,  # 回复时引用对方那条消息
    "always_reply": [],  # 这些会话（人/群）不 @我也一定回：只要最后一条不是我发的就回
    "match_threshold": 0.85,

    "poll_seconds": 20,
    "idle_seconds_required": 30,
    "quiet_hours": ["23:00", "08:00"],
    "confirm_timeout_seconds": 120,
    "max_chats_per_round": 5,
    "max_scroll_pages": 10,
    "context_messages": 20,

    "limits": {
        "max_replies_per_hour": 20,
        "per_chat_cooldown_seconds": 60,
        "reply_delay_seconds": [8, 40],
    },

    "llm": {
        "preset": "claude",
        "provider": "anthropic",
        "base_url": "",
        "model": "claude-opus-5-5",
        "api_key": "",
        "effort": "low",
        "persona": "",
    },

    "layout_overrides": {},
    "web_port": 8765,
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigStore:
    def __init__(self, path=PATH):
        self.path = path
        self._lock = threading.Lock()
        self._cfg = _merge(DEFAULTS, self._read())
        # 旧版配置迁移：以前按「行」滚动、只翻 4 页，现在改成按像素翻 3/4 屏
        if "scroll_lines_per_page" in self._cfg:
            self._cfg.pop("scroll_lines_per_page")
            self._cfg["max_scroll_pages"] = max(10, self._cfg.get("max_scroll_pages", 0))
            self._write(self._cfg)
        # 旧版：这个名单只能填群，现在人和群都能填
        if "groups_reply_without_mention" in self._cfg:
            old = self._cfg.pop("groups_reply_without_mention") or []
            self._cfg["always_reply"] = list(dict.fromkeys((self._cfg.get("always_reply") or []) + old))
            self._write(self._cfg)
        if not os.path.exists(path):
            self._write(self._cfg)

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _write(self, cfg):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)  # 里面可能有 API key，只给自己读
        os.replace(tmp, self.path)

    def get(self):
        with self._lock:
            return copy.deepcopy(self._cfg)

    def update(self, patch):
        with self._lock:
            self._cfg = _merge(self._cfg, patch)
            self._write(self._cfg)
            return copy.deepcopy(self._cfg)
