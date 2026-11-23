"""带界面的入口：菜单栏图标 + 本地设置页 + 后台回复线程。

  .venv/bin/python app.py
启动后菜单栏出现「💬」，点它可以开关、切换模式、打开设置页、退出。
默认是暂停状态，在设置页配好白名单和模型后再打开开关。
"""
import logging
import os
import signal
import sys
import threading
import webbrowser

import rumps

from wxbot import macos, webui
from wxbot.bot import Bot, Status, StatusLogHandler
from wxbot.config import ConfigStore

os.chdir(os.path.dirname(os.path.abspath(__file__)))  # config.json / state.json 放在项目目录


class MenuBarApp(rumps.App):
    def __init__(self, store, status, url, on_quit):
        super().__init__("微信自动回复", title="💬", quit_button=None)
        self.store, self.status, self.url = store, status, url
        self.status_item = rumps.MenuItem("状态：启动中")
        self.toggle_item = rumps.MenuItem("启用自动回复", callback=self.toggle)
        self.confirm_item = rumps.MenuItem("半自动（弹窗确认）", callback=lambda _: self.set_mode("confirm"))
        self.auto_item = rumps.MenuItem("全自动", callback=lambda _: self.set_mode("auto"))
        self.menu = [
            self.status_item, None,
            self.toggle_item, self.confirm_item, self.auto_item, None,
            rumps.MenuItem("打开设置…", callback=lambda _: webbrowser.open(self.url)), None,
            rumps.MenuItem("退出", callback=lambda _: on_quit()),
        ]
        self.refresh(None)
        rumps.Timer(self.refresh, 2).start()

    def toggle(self, _):
        self.store.update({"enabled": not self.store.get()["enabled"]})
        self.refresh(None)

    def set_mode(self, mode):
        self.store.update({"mode": mode})
        self.refresh(None)

    def refresh(self, _):
        cfg = self.store.get()
        self.title = "💬" if cfg["enabled"] else "💬⏸"
        self.toggle_item.state = int(cfg["enabled"])
        self.confirm_item.state = int(cfg["mode"] == "confirm")
        self.auto_item.state = int(cfg["mode"] == "auto")
        self.status_item.title = "状态：" + self.status.snapshot()["text"]


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler("wxbot.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(fh)
    logging.getLogger("wxbot").setLevel(logging.DEBUG)
    for name in ("httpx", "httpx2", "anthropic", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    store, status = ConfigStore(), Status()
    logging.getLogger("wxbot").addHandler(StatusLogHandler(status))

    perms = macos.permissions()
    if not all(perms.values()):
        missing = "、".join(k for k, v in perms.items() if not v)
        rumps.alert("缺少权限", f"请在「系统设置 → 隐私与安全性」里给运行本程序的终端开启：{missing}，然后重新启动。")

    try:
        url, _server = webui.start(store, status)
    except OSError:
        rumps.alert("已经在运行", f"端口 {store.get()['web_port']} 被占用：可能已经开着一个了，看看菜单栏的 💬。")
        return
    print("设置页：", url)
    bot = Bot(store, status)

    def quit_now(*_):
        # 先让所有模拟输入立刻停下，再直接结束进程（不等后台线程）
        bot.stop()
        print("\n已退出")
        os._exit(0)

    signal.signal(signal.SIGINT, quit_now)   # 终端里 Ctrl+C
    signal.signal(signal.SIGTERM, quit_now)
    threading.Thread(target=bot.loop, daemon=True).start()
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    MenuBarApp(store, status, url, quit_now).run()
    quit_now()


if __name__ == "__main__":
    main()
