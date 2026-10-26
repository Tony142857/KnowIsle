"""v1.4 设计令牌、组件去重、按需资产与动效契约。"""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
TEMPLATES = WEB / "templates"
JS = WEB / "static" / "js"
MARKDOWN_PAGES = {"library_chat", "library_review", "board", "post_new", "post_detail"}
PAGE_NAMES = {
    "index", "login", "404", "majors", "major_detail", "course_detail", "resource_detail",
    "document_source", "library", "library_chat", "library_review", "board", "post_new",
    "post_detail", "search", "me", "review", "admin_review", "admin",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_semantic_tokens_v4_cover_surface_text_border_and_status():
    config = read(JS / "tailwind-config.js")
    css = read(WEB / "static/css/main.css")
    tokens = (
        "surface", "primary", "secondary", "muted", "default", "strong",
        "success", "warning", "danger", "info",
    )
    for token in tokens:
        assert token in config
    assert "--surface-page" in css and "--text-primary" in css and "--border-default" in css
    assert "display" in config and "caption" in config and "num" in config and "code" in config
    # 动态 :class 使用的语义类必须在 CSS 提供，避免 Play CDN 只扫描静态 class 导致失效
    for dynamic in ("bg-default", "bg-inverse", "bg-danger-soft", "border-danger-border", "text-disabled"):
        assert f".{dynamic}" in css, dynamic


def test_markdown_assets_load_only_where_needed():
    for name in PAGE_NAMES:
        text = read(TEMPLATES / f"{name}.html")
        assert ("{% set needs_markdown = true %}" in text) is (name in MARKDOWN_PAGES), name


def test_motion_one_is_local_pinned_and_reduced_motion_aware():
    vendor = WEB / "static/vendor/motion-10.18.0.min.js"
    assert vendor.exists()
    digest = hashlib.sha256(vendor.read_bytes()).hexdigest()
    assert digest == "42afe559fafc09469d10a9d7df0978a98e345ed6066ef7fa87451cde0702b7ad"
    assert (WEB / "static/vendor/motion-LICENSE.txt").exists()
    wrapper = read(JS / "motion.js")
    assert "prefers-reduced-motion: reduce" in wrapper
    assert "duration: 0.2" in wrapper
    assert "highlight" in wrapper and "update" in wrapper and "tabs" in wrapper


def test_single_drafts_implementation_with_expiry_and_user_scope():
    api = read(JS / "api.js")
    components = read(JS / "components.js")
    assert "window.ZY.drafts" in api
    assert "window.ZY.drafts" not in components
    assert "7 * 86400000" in api and "zy-user-id" in api


def test_shared_templates_replace_javascript_dom_strings():
    assert 'id="course-card-tpl"' in read(TEMPLATES / "major_detail.html")
    assert 'id="chapter-node-tpl"' in read(TEMPLATES / "course_detail.html")
    assert 'id="library-course-tpl"' in read(TEMPLATES / "library.html")
    assert "ZY.ui.courseCard" in read(JS / "pages/major_detail.js")
    assert "ZY.ui.chapterNode" in read(JS / "pages/course_detail.js")
    assert "ZY.ui.fromTemplate('library-course-tpl')" in read(JS / "pages/library.js")
    assert "ZY.ui.iconSvg('star-solid'" in read(JS / "pages/resource_detail.js")
    assert 'id="comment-node-tpl"' in read(TEMPLATES / "post_detail.html")


def test_dynamic_alpine_directives_use_valid_dom_attribute_names():
    for path in JS.rglob("*.js"):
        assert not re.search(r"setAttribute\(['\"]@", read(path)), path


def test_admin_has_one_responsive_data_structure_per_list():
    admin = read(TEMPLATES / "admin.html")
    assert admin.count("responsive-table") >= 4
    assert "md:hidden" not in admin


def test_rich_pages_use_shared_page_header_and_component_macros_exist():
    for name in ("resource_detail", "post_detail", "board", "search"):
        assert "page_header(" in read(TEMPLATES / f"{name}.html")
    components = read(TEMPLATES / "_components.html")
    macros = (
        "status_badge", "empty_state", "pagination", "tabs",
        "form_field", "filter_bar", "modal", "list_row",
    )
    for macro in macros:
        assert f"macro {macro}" in components


def test_accessibility_motion_and_forced_colors_contracts():
    css = read(WEB / "static/css/main.css")
    assert "prefers-reduced-motion: reduce" in css
    assert "forced-colors: active" in css
    assert "min-height: 2.75rem" in css
    course = read(JS / "pages/course_detail.js")
    admin = read(JS / "pages/admin.js")
    assert "nextTab" in course and "firstTab" in course
    assert "nextTab" in admin and "lastTab" in admin


def test_semantic_tokens_replace_legacy_palette_utilities():
    legacy = re.compile(
        r"(?:text|bg|border|divide|ring|from|to|via|hover:bg|hover:text)-"
        r"(?:slate|gray|zinc|neutral|red|emerald|amber|green|blue|indigo|sky|rose|orange|yellow|teal|cyan)"
        r"-\d{2,3}"
    )
    for path in list(TEMPLATES.glob("*.html")) + list(JS.rglob("*.js")):
        assert not legacy.search(read(path)), path


def test_large_vendors_load_on_demand():
    admin = read(TEMPLATES / "admin.html")
    assert "echarts-5.6.0.min.js" not in admin
    script = read(JS / "pages/admin.js")
    assert "ensureEcharts" in script and "echarts-5.6.0.min.js" in script
    assert "tab !== 'dashboard'" in script
    base = read(TEMPLATES / "base.html")
    assert "motion-10.18.0.min.js" not in base
    assert "motion.js" in base


def test_templates_keep_csp_and_no_runtime_external_assets():
    for name in PAGE_NAMES | {"base", "_components", "_page_header", "_icons", "_illustrations", "_precheck"}:
        text = read(TEMPLATES / f"{name}.html")
        assert not re.search(r"(?<![:\w-])style\s*=", text), name
        inline_handlers = r"(?<![@\w:-])on(?:click|change|submit|load|error|keydown|input)\s*="
        assert not re.search(inline_handlers, text), name
        for tag in re.findall(r"<script\b[^>]*>", text):
            assert "src=" in tag or 'type="application/json"' in tag, (name, tag)
        assert not re.search(r"(?:src|href)=[\"']https?://", text), name


def test_favicon_uses_emerald_brand():
    favicon = read(WEB / "static/favicon.svg")
    assert "#047857" in favicon
    assert "#0284c7" not in favicon
