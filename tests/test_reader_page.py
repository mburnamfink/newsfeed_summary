from newsfeed import reader_page
from newsfeed.reader_page import _clamp_font_sizes, render_article


def test_clamp_raises_small_sizes_and_keeps_large():
    assert _clamp_font_sizes("font-size:12px") == "font-size:18px"
    assert _clamp_font_sizes("font-size:10pt") == "font-size:18px"
    assert _clamp_font_sizes("font-size: 24px") == "font-size: 24px"
    assert _clamp_font_sizes("font-size:0.8em") == "font-size:1em"
    assert _clamp_font_sizes("font-size:85%") == "font-size:1em"
    assert _clamp_font_sizes("font-size:1.6em") == "font-size:1.6em"
    assert _clamp_font_sizes("font-size:x-small") == "font-size:1em"
    assert _clamp_font_sizes("font: bold 12px/1.4 Arial") == "font: bold 18px/1.4 Arial"
    assert _clamp_font_sizes("margin:0 0 10px") == "margin:0 0 10px"


def test_render_article_readable_font_rewrites_html():
    src = (
        "<html><head><style>.tiny{font-size:11px}</style></head><body>"
        "<p style='font-size:13px;color:#999'>small</p>"
        "<h1 style='font-size:32px'>Big</h1><font size='1' face='Verdana'>x</font></body></html>"
    )
    out = render_article(src, "/archive/d/m/", readable_font=True)
    assert "font-size:11px" not in out and "font-size:13px" not in out
    assert "font-size:32px" in out  # headings keep their hierarchy
    assert "Verdana" not in out
    assert "Georgia" in out


def test_render_article_default_leaves_fonts_alone():
    src = "<html><head></head><body><p style='font-size:13px'>small</p></body></html>"
    out = render_article(src, "/archive/d/m/")
    assert "font-size:13px" in out
    assert reader_page._READABLE_CSS not in out
