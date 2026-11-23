"""命令行方式运行（不带界面）。平时用 app.py。

  .venv/bin/python run.py              # 按 config.json 运行（需要 enabled=true）
  .venv/bin/python run.py --dry-run    # 演练：只滚动列表，不点开会话、不发送；对当前打开的会话生成草稿
  加 -v 看详细日志，Ctrl+C 停止
"""
import logging
import sys

from wxbot import macos
from wxbot.bot import Bot
from wxbot.config import ConfigStore


def main():
    logging.basicConfig(level=logging.DEBUG if "-v" in sys.argv else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler("wxbot.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(fh)
    logging.getLogger("wxbot").setLevel(logging.DEBUG)
    for name in ("httpx", "httpx2", "anthropic", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    perms = macos.permissions()
    if not all(perms.values()):
        print("缺少权限：", [k for k, v in perms.items() if not v],
              "\n请在「系统设置 → 隐私与安全性」里给运行本程序的终端开启。")
        sys.exit(1)
    try:
        Bot(ConfigStore(), dry_run="--dry-run" in sys.argv).loop()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
