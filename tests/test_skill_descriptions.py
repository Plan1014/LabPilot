from src.agent.tools import SkillLoader


def test_folded_description_is_parsed():
    text = "---\nname: x\ndescription: >\n  第一行\n  第二行\n---\n# body\n"
    meta, body = SkillLoader._parse_frontmatter(text)
    assert "第一行" in meta["description"]
    assert "第二行" in meta["description"]


def test_plain_description_still_works():
    text = "---\nname: x\ndescription: 简单描述\n---\n# body\n"
    meta, body = SkillLoader._parse_frontmatter(text)
    assert meta["description"] == "简单描述"


def test_body_is_kept():
    text = "---\nname: x\ndescription: d\n---\n# 正文\n"
    meta, body = SkillLoader._parse_frontmatter(text)
    assert "正文" in body


def test_literal_folded_marker_is_not_the_description():
    """`description: >` 不该被当成描述内容本身。"""
    text = "---\nname: x\ndescription: >\n  真正的描述\n---\n# body\n"
    meta, _ = SkillLoader._parse_frontmatter(text)
    assert meta["description"] != ">"
