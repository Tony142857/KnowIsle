"""v1.3 I3/I6/I7/I8 页面结构与恢复路径的轻量契约测试。"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "web" / "templates"
PAGES = ROOT / "app" / "web" / "static" / "js" / "pages"
OWNED_TEMPLATES = [
    "404.html", "majors.html", "major_detail.html", "course_detail.html",
    "resource_detail.html", "board.html", "post_new.html", "post_detail.html",
    "me.html", "search.html", "review.html", "admin_review.html", "admin.html",
]
OWNED_SCRIPTS = [
    "majors.js", "major_detail.js", "course_detail.js", "resource_detail.js",
    "board.js", "post_new.js", "post_detail.js", "me.js", "search.js",
    "review.js", "admin_review.js", "admin.js",
]


def template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def script(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", OWNED_TEMPLATES)
def test_owned_templates_have_no_inline_handlers_or_style(name):
    text = template(name)
    assert not re.search(r"(?<![:\w-])style\s*=", text)
    assert not re.search(r"(?<![@\w:-])on(?:click|change|submit|load|error|keydown|input)\s*=", text)
    for tag in re.findall(r"<script\b[^>]*>", text):
        assert "src=" in tag or 'type="application/json"' in tag


@pytest.mark.parametrize("name", OWNED_SCRIPTS)
def test_static_scripts_do_not_expect_jinja_rendering(name):
    assert "{{" not in script(name)
    assert "{%" not in script(name) or name in {"board.js", "post_detail.js", "resource_detail.js"}


def test_course_resource_state_is_url_recoverable_and_paginated():
    text = template("course_detail.html") + script("course_detail.js")
    for token in ["URLSearchParams", "popstate", "pushState", "chapter_id", "page", "size", "params.set('q'"]:
        assert token in text
    assert "data-chapter-title" in text
    assert "重试" in text and "资料分页" in text


def test_resource_download_and_ownership_guards():
    html = template("resource_detail.html")
    js = script("resource_detail.js")
    assert 'href="/api/resources/{{ resource.id }}/download"' not in html
    assert "resource.uploader_id != user.id" in html
    assert "ZY.report('resource'" in html
    assert "response.blob()" in js and "下载后余额" in js
    assert "if (after < 0)" in js and "isSelf" in js


def test_post_editor_and_detail_cover_recovery_and_acceptance_rules():
    editor = template("post_new.html") + script("post_new.js")
    detail = template("post_detail.html") + script("post_detail.js")
    api = (ROOT / "app/web/static/js/api.js").read_text(encoding="utf-8")
    assert "ZY.drafts" in editor and "beforeunload" in editor
    assert "localStorage" in api  # v1.4 草稿真实落盘统一由 api.js 的 ZY.drafts 承担
    assert "Markdown 预览" in editor and "course_id" in editor
    assert "确认托管悬赏" in editor
    assert "post.board == 'qa' or (c.author_id != post.author_id and not post.accepted_comment_id)" in detail
    assert "post.board == 'qa' or c.author_id != post.author_id" in detail
    assert "return !!pd.is_author && pd.board === 'qa'" in detail
    assert "handleAccepted" in detail and "initiallyAcceptable" in detail
    assert "/ai-retry" in detail and "deadline" in detail
    assert "ZY.report('post'" in detail and "ZY.report('comment'" in detail


def test_me_and_search_expose_full_lists_and_chinese_labels():
    me = template("me.html") + script("me.js")
    search = template("search.html") + script("search.js")
    assert "加载更多通知" in me and "加载更多收藏" in me and "加载更多关注" in me
    assert "updateUnreadBadge(-1)" in me
    assert "danger: true" in me
    assert "bounty_escrow" in me and "experience: '经验长廊'" in me
    assert "major_id" in search and "majorOptions" in search
    assert "clearAll" in search and "loadMore('post')" in search and "loadMore('resource')" in search


def test_review_pages_are_lazy_confirmed_and_keep_reassigned_tasks():
    review = template("review.html") + script("review.js")
    admin = template("admin_review.html") + script("admin_review.js")
    assert "previewAttempts" in review and "30000" in review
    assert "确认协审通过" in review and "review-queue-count" in review
    assert "展开并加载预览" in admin
    assert '<iframe src="{{ t.preview_url }}"' not in admin
    assert "data-slot=\"review-state\"" in admin
    assert "if (data.escalated) removeCoTaskRow(taskId)" in admin


def test_admin_tabs_lazy_loading_pagination_and_accessible_chart():
    html = template("admin.html")
    js = script("admin.js")
    assert "adminTabs()" in html and "popstate" in js
    assert "@admin:tab.window" in html
    assert 'x-data="adminCourses()" x-init="load()"' not in html
    assert "课程分页" in html and "用户分页" in html
    assert "addEventListener('resize'" in js
    assert "查看趋势数据表" in html and "<caption" in html
