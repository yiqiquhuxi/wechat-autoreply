"""生成回复。支持 Claude（Anthropic SDK）和任意兼容 OpenAI 接口的模型（DeepSeek、通义、Kimi、Ollama 等）。"""
import datetime as dt
import logging
import os
import re

log = logging.getLogger("wxbot")

SKIP = "[SKIP]"

BASE_RULES = f"""你在替用户回复微信消息。规则：
- 用用户的口吻，简短自然，像真人打字，一般一两句话；不要用 markdown、不要列表、不要自称 AI。
- 只回复对方最新发来的、还没被回复的内容。
- 遇到你没把握的事（约时间、答应事情、报价、转账、要隐私信息、涉及工作决定），不要替用户做决定，回复类似「我晚点看下回你」这样的缓冲话。
- 如果这条消息不需要回复（比如对方只发了「好的」「嗯」、表情、系统通知），只输出 {SKIP}。
- 记录里带时间；隔得久远的内容可能不是同一个话题，别把昨天的旧事当成刚刚发生。
- 群聊里没识别出发送者的消息会写成「群成员」，不确定是谁说的就别硬认。
- 对方只发了［图片］［语音］［视频］［文件］这类你看不到内容的消息时，别猜内容，回一句很短的话（比如「收到」「啥图」）或输出 {SKIP}。
- 对方只发了［表情］/［动画表情］时，可以只回一个合适的 emoji（比如 😂、👍），不用配文字。
- 只输出要发送的那句话本身，不要任何解释。"""

MAX_GAP = dt.timedelta(hours=4)


def _parse_at(at):
    try:
        return dt.datetime.fromisoformat(at) if at else None
    except (TypeError, ValueError):
        return None


def _fmt_at(at, now):
    t = _parse_at(at)
    if not t:
        return ""
    if t.date() == now.date():
        return t.strftime("%H:%M")
    if t.date() == (now - dt.timedelta(days=1)).date():
        return "昨天 " + t.strftime("%H:%M")
    return t.strftime("%m-%d %H:%M")


def _gap(prev, cur):
    sec = (cur - prev).total_seconds()
    if sec >= 86400:
        return f"隔了 {sec / 86400:.1f} 天"
    return f"隔了 {sec / 3600:.1f} 小时"


def _who(m, chat_name, is_group):
    if m.get("me"):
        return "我"
    if not is_group:
        return chat_name
    sender = re.sub(r"[。、，,;；:：]+", " ", (m.get("sender") or ""))
    sender = re.sub(r"\s+", " ", sender).strip(" 。.、,，:：")
    # OCR 出来的昵称经常带标点杂讯，看着不像名字就当没识别出来
    if sender and len(sender) <= 24 and re.fullmatch(r"[\w一-鿿·\-.()（） ]+", sender):
        return sender
    return "群成员"


def _history_part(messages, fresh):
    """把 messages 末尾和 fresh 重合的部分切掉，返回（历史, 匹配到的新消息）。
    新消息在历史里只出现一次，同时还能拿到它的时间，用来标记「隔了多久」。"""
    i = len(messages)
    for fm in reversed(fresh or []):
        if (i > 0 and messages[i - 1].get("text") == fm.get("text")
                and bool(messages[i - 1].get("me")) == bool(fm.get("me"))):
            i -= 1
        else:
            break
    return messages[:i], messages[i:]


def build_prompt(llm_cfg, chat_name, is_group, messages, my_name, must_reply=False, new_messages=None):
    now = dt.datetime.now()
    fresh = list(new_messages or [])
    hist, matched = _history_part(messages, fresh)

    lines = []
    prev = None
    for m in hist:
        t = _parse_at(m.get("at"))
        if prev and t and (t - prev) > MAX_GAP:
            lines.append(f"—— {_gap(prev, t)} ——")
        ts = _fmt_at(m.get("at"), now)
        lines.append(f"{f'[{ts}] ' if ts else ''}{_who(m, chat_name, is_group)}: {m['text']}")
        if t:
            prev = t

    fresh_lines = [f"{_who(m, chat_name, is_group)}: {m['text']}" for m in fresh]
    fresh_at = _parse_at(matched[0].get("at")) if matched else None
    if prev and fresh_at and (fresh_at - prev) > MAX_GAP:
        fresh_lines.insert(0, f"—— {_gap(prev, fresh_at)} ——")
    kind = f"群聊「{chat_name}」" if is_group else f"和「{chat_name}」的私聊"
    persona = llm_cfg.get("persona", "").strip()
    system = BASE_RULES + (f"\n\n关于用户本人：\n{persona}" if persona else "")
    if must_reply:
        system += ("\n\n这个会话要求「最后一条不是我发的就一定要回」：这次必须发一句简短自然的话，"
                   f"就算你觉得没必要也不要输出 {SKIP}。")
    user = (f"现在是 {now:%Y-%m-%d %H:%M}。这是{kind}的聊天记录（屏幕识别得到，可能有错字）。"
            f"用户在群里的昵称是「{my_name}」。\n\n"
            "【之前的聊天记录】\n" + ("\n".join(lines) if lines else "（无）") + "\n\n"
            "【对方刚发来、还没回复的消息】\n" + ("\n".join(fresh_lines) if fresh_lines else "（无）") + "\n\n"
            "请只针对【对方刚发来】的消息，给出用户接下来要发的回复。")
    return system, user


def _call_anthropic(c, system, user):
    import anthropic
    client = anthropic.Anthropic(api_key=c.get("api_key") or None,  # 空则读 ANTHROPIC_API_KEY
                                 base_url=c.get("base_url") or None)
    resp = client.beta.messages.create(
        model=c["model"],
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # 被安全策略拒答时，服务端自动换模型重试
        output_config={"effort": c.get("effort") or "low"},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        return None
    return "".join(b.text for b in resp.content if b.type == "text")


def _call_openai_compatible(c, system, user):
    import openai
    if not c.get("base_url"):
        raise ValueError("没填接口地址 base_url")
    client = openai.OpenAI(api_key=c.get("api_key") or os.environ.get("LLM_API_KEY") or "none",
                           base_url=c["base_url"], timeout=60)
    resp = client.chat.completions.create(
        model=c["model"],
        max_tokens=1000,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content or ""


def generate(llm_cfg, chat_name, is_group, messages, my_name, must_reply=False, new_messages=None):
    """返回要发的文本；不需要回复时返回 None。出错直接抛异常，由调用方记录。
    new_messages 是这次真正要回的新消息，会和历史分开呈现给模型。"""
    if not llm_cfg.get("model"):
        raise ValueError("没填模型名")
    system, user = build_prompt(llm_cfg, chat_name, is_group, messages, my_name, must_reply, new_messages)
    if os.environ.get("WXBOT_DUMP_PROMPT"):
        log.info("提示词 → %s\n%s", chat_name, user)
    call = _call_anthropic if llm_cfg.get("provider") == "anthropic" else _call_openai_compatible
    text = call(llm_cfg, system, user) or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # 部分推理模型会把思考过程写进正文
    if not text or SKIP in text:
        return None
    return text

def list_models(c):
    """向服务商拉取可用模型名列表。"""
    if c.get("provider") == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=c.get("api_key") or None,
                                     base_url=c.get("base_url") or None, timeout=20)
        out, after = [], None
        while True:
            kw = {"limit": 1000}
            if after:
                kw["after_id"] = after
            page = client.models.list(**kw)
            out += [m.id for m in page.data]
            if not getattr(page, "has_more", False) or not page.data or len(out) >= 500:
                return out
            after = page.data[-1].id
    import openai
    if not c.get("base_url"):
        raise ValueError("没填接口地址 base_url")
    client = openai.OpenAI(api_key=c.get("api_key") or os.environ.get("LLM_API_KEY") or "none",
                           base_url=c["base_url"], timeout=20)
    return sorted(m.id for m in client.models.list().data)
