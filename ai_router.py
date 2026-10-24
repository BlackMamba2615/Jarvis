from local_llm import ask_local


def should_use_cloud(text: str) -> bool:
    """
    第一版本不让 AI 自己决定是否调用 ChatGPT，
    先使用简单可靠的规则。
    """

    text_lower = text.lower()

    cloud_keywords = [
        "深入分析",
        "深度分析",
        "详细分析",
        "认真推理",
        "复杂推理",
        "联网搜索",
        "搜索最新",
        "查一下最新",
        "最新消息",
        "最新新闻",
        "写完整项目",
        "重构整个项目",
        "复杂代码",
        "使用chatgpt",
        "用chatgpt",
        "问chatgpt",
        "云端模型",
    ]

    return any(
        keyword in text_lower
        for keyword in cloud_keywords
    )


def ask_ai(text: str):
    if should_use_cloud(text):

        return {
            "provider": "chatgpt",
            "text": None,
        }

    result = ask_local(
        text,
        think=False,
    )

    return {
        "provider": "local",
        "text": result,
    }


if __name__ == "__main__":

    tests = [
        "你好",
        "帮我翻译一句话",
        "打开微信",
        "深入分析一下我的Jarvis架构",
        "搜索最新AI新闻",
    ]

    for text in tests:

        result = ask_ai(text)

        print()
        print("用户：", text)
        print("路由：", result["provider"])

        if result["text"]:
            print("回答：", result["text"])