"""The reasoning panel must not render literal ``\\n`` as text.

2026-08-20 生产事故(sunke 的 Adobe 开号会话):飞书卡片的推理面板显示了满屏字面
``\\n`` + 工具叙述 + repr 碎片,持续约 160 秒,直到真答案落库把卡片刷掉。

根因链:
1. 网关 ``auto`` 路由背后是 reasoning 模型(deepseek / qwen / kimi),推理走
   **结构化 ``reasoning_content`` 字段**,到卡片层是裸模型文本、没有 ``<think>`` 标签;
2. ``_split_reasoning_text`` 只认标签 → ``reasoning_text`` 为空;
3. ``state["reasoning"] = reasoning_text or answer_text`` 于是把整段原始推理流塞进面板;
4. 原始模型文本把自己的换行转义成了两个字符 ``\\`` + ``n``,面板照原样渲染。

修法是 display-only 的:只反转义推理面板要显示的文本。落库的答案在那次事故里本来就是
对的(``messages`` 里 394 字符的正确回复),所以这里绝不碰持久化 / 回放 / 回传给 provider
的内容。面板长度由 ``builder`` 侧已有的 ``_clip(reasoning, 1200)`` 兜着,不在本单范围。
"""

from hermes_multitenancy.card.sanitization import _unescape_display_whitespace


class TestUnescapeDisplayWhitespace:
    def test_literal_newline_becomes_a_real_break(self):
        assert _unescape_display_whitespace("第一行\\n第二行") == "第一行\n第二行"

    def test_crlf_collapses_to_one_break(self):
        assert _unescape_display_whitespace("上一行\\r\\n下一行") == "上一行\n下一行"

    def test_lone_cr_becomes_a_break(self):
        assert _unescape_display_whitespace("上一行\\r 下一行") == "上一行\n 下一行"

    def test_escape_glued_to_a_word_is_left_alone(self):
        """Deliberate limitation — the lookahead is what protects real backslashes.

        ``a\\nb`` is ambiguous; ``C:\\new`` and ``re.sub(r"\\n+")`` are not
        whitespace. A genuine escaped break is followed by whitespace, CJK,
        punctuation, another escape, or end of string — never a word character.
        """
        assert _unescape_display_whitespace("a\\nb") == "a\\nb"

    def test_literal_tab_is_not_touched(self):
        """``\\t`` collides with too many ordinary words for a cosmetic gain."""
        assert _unescape_display_whitespace("a\\t b") == "a\\t b"

    def test_blank_wall_collapses(self):
        """The incident card showed a wall of standalone literal ``\\n``."""
        out = _unescape_display_whitespace("头\\n\\n\\n\\n\\n\\n尾")
        assert out == "头\n\n尾"

    def test_incident_shape(self):
        """Reproduces the reported card text shape end to end."""
        raw = (
            "发送通知邮件\\n\\n"
            "密码: 已发送到 zhanghailong@example.com\\n"
            "（密送 it@example.com）\\n\\n\\n\\n"
        )
        out = _unescape_display_whitespace(raw)
        assert "\\n" not in out, f"literal escape survived: {out!r}"
        assert out.startswith("发送通知邮件")
        assert "密送 it@example.com" in out
        assert "\n\n\n" not in out


class TestUnescapeGuards:
    def test_text_without_backslash_is_returned_unchanged(self):
        """Fast path — must not touch normal markdown, including real newlines."""
        s = "# 标题\n\n| 项 | 值 |\n|---|---|\n| 邮箱 | a@b.com |\n"
        assert _unescape_display_whitespace(s) == s

    def test_real_newlines_survive(self):
        assert _unescape_display_whitespace("a\nb") == "a\nb"

    def test_unrelated_backslashes_survive(self):
        r"""Only ``\n`` / ``\r`` count, and only off a word boundary; leave the rest."""
        s = r"C:\Users\test and \d+ and \\"
        assert _unescape_display_whitespace(s) == s

    def test_empty_and_none(self):
        assert _unescape_display_whitespace("") == ""
        assert _unescape_display_whitespace(None) == ""

    def test_idempotent(self):
        once = _unescape_display_whitespace("a\\nb")
        assert _unescape_display_whitespace(once) == once


class TestStreamingControllerWiring:
    """Both sites that assign ``state["reasoning"]`` from model text must unescape."""

    def test_both_assignment_sites_go_through_the_helper(self):
        import inspect
        from hermes_multitenancy.card import streaming_controller as sc

        src = inspect.getsource(sc)
        assigns = [
            line.strip()
            for line in src.splitlines()
            if 'state["reasoning"]' in line and "=" in line and "==" not in line
        ]
        # Only the two model-text sites plus the terminal reset (`= ""`).
        model_text_assigns = [a for a in assigns if a != 'state["reasoning"] = ""']
        assert model_text_assigns, "no reasoning assignments found — did the file move?"
        for a in model_text_assigns:
            assert "_unescape_display_whitespace" in a, f"unescaped reasoning site: {a}"
