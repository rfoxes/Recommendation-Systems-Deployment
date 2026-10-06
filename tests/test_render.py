import re

from app.serving.render import PLACEHOLDERS, AdTemplate, escape_js_string

VALUES = {
    "CHAR_NAME": "Luna",
    "CAMPAIGN": "Baba Casino",
    "CHAR_MESSAGE": "Spin to win!",
    "CTA": "Play Free",
    "MEDIA_URL": "https://cdn.example.com/luna.mp4",
    "TRACKING_URL": "https://apps.apple.com/app/id1",
    "IMPRESSION_URL": "",
    "AD_ID": "imp_123",
    "API_URL": "https://api.example.com",
    "API_KEY": "click-key",
    "THEME": "dark",
    "DOWNLOADS": "1.2M",
}


def js_const(html: str, name: str) -> str:
    match = re.search(rf'const {name}\s*=\s*"(.*?)";', html)
    assert match, name
    return match.group(1)


def test_every_placeholder_is_filled() -> None:
    html = AdTemplate().render(VALUES)
    assert "{{" not in html and "}}" not in html
    assert set(VALUES) == PLACEHOLDERS
    assert js_const(html, "AD_ID") == "imp_123" and js_const(html, "CTA") == "Play Free"


def test_video_or_image_section() -> None:
    template = AdTemplate()
    video = template.render(VALUES)
    image = template.render({**VALUES, "MEDIA_URL": "https://cdn.example.com/luna.png"})
    assert '<video src="https://cdn.example.com/luna.mp4"' in video and '<img src="https://cdn.example.com' not in video
    assert '<img src="https://cdn.example.com/luna.png"' in image and "<video src=" not in image


def test_llm_text_cannot_break_out_of_the_script() -> None:
    evil = 'Hi "friend" </script><script>alert(1)</script>\n& more'
    html = AdTemplate().render({**VALUES, "CHAR_MESSAGE": evil, "CHAR_NAME": '<b>"Rex"</b>'})
    assert "<script>alert(1)" not in html
    assert js_const(html, "CHAR_MESSAGE") == escape_js_string(evil)
    assert "<title>&lt;b&gt;&quot;Rex&quot;&lt;/b&gt; · Sponsored</title>" in html  # HTML context escaping


def test_js_escaping_round_trips() -> None:
    import json

    text = 'quote " backslash \\ newline \n tag </script> amp & unicode é'
    assert json.loads(f'"{escape_js_string(text)}"') == text
