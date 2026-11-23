"""生成回复。支持 Claude（Anthropic SDK）和任意兼容 OpenAI 接口的模型（DeepSeek、通义、Kimi、Ollama 等）。"""
import os
import re

SKIP = "[SKIP]"

BASE_RULES = f"""你在替用户回复微信消息。规则：
- 用用户的口吻，简短自然，像真人打字，一般一两句话；不要用 markdown、不要列表、不要自称 AI。
- 只回复对方最新发来的、还没被回复的内容。
- 遇到你没把握的事（约时间、答应事情、报价、转账、要隐私信息、涉及工作决定），不要替用户做决定，回复类似「我晚点看下回你」这样的缓冲话。
- 如果这条消息不需要回复（比如对方只发了「好的」「嗯」、表情、系统通知），只输出 {SKIP}。
- 对方只发了［图片］［语音］［视频］这类你看到内容的消息时，别猜内容，回一句很短的话（比如「收到」「啥图」）或输出 {SKIP}。
- 只输出要发送的那句话本身，不要任何解释。"""


def build_prompt(llm_cfg, chat_name, is_group, messages, my_name, must_reply=False):
    lines = []
    for m in messages:
        who = "我" if m["me"] else (m.get("sender") or ("群成员" if is_group else chat_name))
        lines.append(f"{who}: {m['text']}")
    kind = f"群聊「{chat_name}」" if is_group else f"和「{chat_name}」的私聊"
    persona = llm_cfg.get("persona", "").strip()
    system = BASE_RULES + (f"\n\n关于用户本人：\n{persona}" if persona else "")
    if must_reply:
        system += ("\n\n这个会话要求「最后一条不是我发的就一定要回」：这次必须发一句简短自然的话，"
                   f"就算你觉得没必要也不要输出 {SKIP}。")
    user = (f"这是{kind}最近的聊天记录（屏幕识别得到，可能有错字）。用户在群里的昵称是「{my_name}」。\n\n"
            + "\n".join(lines) + "\n\n请给出用户接下来要发的回复。")
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


def generate(llm_cfg, chat_name, is_group, messages, my_name, must_reply=False):
    """返回要发的文本；不需要回复时返回 None。出错直接抛异常，由调用方记录。"""
    if not llm_cfg.get("model"):
        raise ValueError("没填模型名")
    system, user = build_prompt(llm_cfg, chat_name, is_group, messages, my_name, must_reply)
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
