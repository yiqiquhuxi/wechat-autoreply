"""只读探测：看这台机器上的微信能读到什么。不点击、不发送。

用法: .venv/bin/python probe.py [--full]
默认把识别出的文字截断显示，--full 显示全文。截图保存在 probe_out/。
"""
import collections
import os
import sys

import ApplicationServices as AS

from wxbot import macos
from wxbot.wechat import attach_badges, detect_layout, find_avatars, find_badges, parse_chat, parse_session_rows

FULL = "--full" in sys.argv


def short(s, n=8):
    return s if FULL or len(s) <= n else s[:n] + "…"


def ax_probe(pid, max_nodes=4000):
    app = AS.AXUIElementCreateApplication(pid)
    err, wins = AS.AXUIElementCopyAttributeValue(app, "AXWindows", None)
    if err or not wins:
        return None
    roles = collections.Counter()
    texts = []
    stack = [(w, 0) for w in wins]
    n = 0
    while stack and n < max_nodes:
        el, d = stack.pop()
        n += 1
        _, role = AS.AXUIElementCopyAttributeValue(el, "AXRole", None)
        roles[str(role)] += 1
        for attr in ("AXValue", "AXTitle", "AXDescription"):
            e, v = AS.AXUIElementCopyAttributeValue(el, attr, None)
            if not e and isinstance(v, str) and v.strip():
                texts.append((str(role), attr, v.strip()))
        e, kids = AS.AXUIElementCopyAttributeValue(el, "AXChildren", None)
        if not e and kids:
            stack.extend((k, d + 1) for k in kids)
    return n, roles, texts


def main():
    print("== 权限 ==")
    for k, v in macos.permissions().items():
        print(f"  {k}: {'已授权' if v else '未授权'}")

    print("\n== Dock 角标（总未读数）==")
    print("  ", macos.dock_badge())

    pid = macos.wechat_pid()
    print("\n== 微信进程 ==  pid:", pid)
    if not pid:
        return

    print("\n== 辅助功能接口能读到的界面元素 ==")
    r = ax_probe(pid)
    if r is None:
        print("  读不到窗口")
    else:
        n, roles, texts = r
        print(f"  节点数 {n}，角色分布 {dict(roles.most_common(10))}")
        print(f"  带文字的节点 {len(texts)} 个，前 15 个：")
        for role, attr, v in texts[:15]:
            print(f"    {role}.{attr} = {short(v)}")

    win = macos.wechat_window()
    print("\n== 微信主窗口 ==", win)
    if not win:
        print("  窗口不在屏幕上（最小化/被遮挡在别的桌面？），截图识别部分跳过")
        return

    img, scale = macos.screenshot(win)
    os.makedirs("probe_out", exist_ok=True)
    img.save("probe_out/window.png")
    print(f"  截图 {img.width}x{img.height}px，scale={scale}，已存 probe_out/window.png")

    lines = macos.ocr(img, scale)
    print(f"\n== OCR 识别到 {len(lines)} 行 ==")

    layout = detect_layout(img, scale, win)
    print("\n== 自动识别的布局（pt，相对窗口左上角）==")
    print("  ", layout)

    print("\n== 会话列表 ==")
    avatars = find_avatars(img, scale, layout)
    print(f"  找到头像 {len(avatars)} 个")
    rows = parse_session_rows(lines, layout, avatars)
    badges = find_badges(img, scale, layout)
    unread = {r["y"] for r in attach_badges(rows, badges)}
    for row in rows[:20]:
        flag = " 🔴未读" if row["y"] in unread else ""
        print(f"  头像y={row['top']:.0f}~{row['bottom']:.0f}  名称={short(row['name'])}  预览={short(row.get('preview', ''), 6)}{flag}")

    print("\n== 红色色块（候选红点）==")
    for b in badges:
        print(f"  y={b['y']:.0f} x={b['x']:.0f} 半径≈{b['r']:.0f}pt")

    print("\n== 当前打开的聊天 ==")
    chat = parse_chat(lines, layout)
    print(f"  标题={short(chat['title'] or '(未识别)', 12)}  群聊={chat['is_group']}")
    for m in chat["messages"][-12:]:
        who = "我" if m["me"] else (m.get("sender") or "对方")
        print(f"    [{short(who, 6)}] {short(m['text'], 10)}")


if __name__ == "__main__":
    main()
