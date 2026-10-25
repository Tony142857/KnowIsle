"""v1.3 学习旅程前端契约：可访问性、失败恢复与真实模板渲染。"""

from pathlib import Path

from app.storage.models import Course, Document

from .fakestack import FakeResult, FakeSession, http_client, make_chunk, make_course, make_doc, make_user

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app/web/templates"
JS = ROOT / "app/web/static/js"
USER = make_user(1)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_global_accessibility_and_report_contract():
    base = _read(TEMPLATES / "base.html")
    components = _read(JS / "components.js")
    api = _read(JS / "api.js")
    assert 'href="#main-content"' in base
    assert 'id="main-content" tabindex="-1"' in base
    assert "aria-current=" in base
    assert "aria-controls=\"mobile-navigation\"" in base
    assert "mobileOpen ? '关闭导航菜单' : '打开导航菜单'" in base
    assert 'x-data="reportModal()"' in base
    assert 'aria-labelledby="zy-report-title"' in base
    assert "window.ZY.report = function (targetType, targetId)" in components
    assert "ZY.api.post('/api/reports'" in components
    assert "data.created === false" in components
    assert "type === 'error' ? 'alert' : 'status'" in components
    assert "if (type === 'error') return;" in components
    assert "encodeURIComponent(next)" in api
    assert "'/login?next='" in api


def test_login_uses_native_form_and_preserves_safe_next():
    template = _read(TEMPLATES / "login.html")
    script = _read(JS / "pages/login.js")
    assert '<form @submit.prevent="step === 1 ? sendCode() : verify()"' in template
    assert 'type="submit"' in template
    assert 'for="login-student-no"' in template
    assert 'for="login-email"' in template
    assert 'aria-describedby="login-email-help login-error"' in template
    assert 'role="alert"' in template
    assert "this.focusStep()" in script
    assert "window.location.href = this.next" in script
    assert "!value.startsWith('//')" in script
    assert "!value.includes('\\\\')" in script


def test_library_upload_and_recovery_contract():
    template = _read(TEMPLATES / "library.html")
    script = _read(JS / "pages/library.js")
    assert "@drop.prevent=\"selectDropped" in template
    assert 'accept=".pdf,.pptx,.docx,.md,.markdown"' in template
    assert "<progress" in template and ":value=\"uploadProgress\"" in template
    assert "data-document-id" in template
    assert "选择同文件重传" in template
    assert "投稿进度：预检 → 协审 → 终审 → 上架" in template
    assert "res.review_reason" in template
    assert "new XMLHttpRequest()" in script
    assert "xhr.upload.addEventListener('progress'" in script
    assert "50 * 1024 * 1024" in script
    assert "document.querySelectorAll('[data-document-id]')" in script
    assert "POLL_TIMEOUT" in script and "Math.pow(2, state.failures - 1)" in script
    assert "data.dedup && existingRow" in script
    assert "'/preview/retry'" in script
    assert "'/submit/retry'" in script


def test_chat_failure_sse_and_reading_contract():
    template = _read(TEMPLATES / "library_chat.html")
    script = _read(JS / "pages/library_chat.js")
    for label in ("停止", "新对话", "复制回答", "保留问题并重试", "回到最新回答"):
        assert label in template
    assert "@compositionstart=\"composing = true\"" in template
    assert "/api/users/me/quota" in script
    assert "new AbortController()" in script
    assert "signal: controller.signal" in script
    assert "evt.type === 'done'" in script and "evt.type === 'error'" in script
    assert "this.question = q" in script
    assert "event.isComposing || this.composing" in script
    assert "el.scrollHeight - el.scrollTop - el.clientHeight < 56" in script


def test_review_limits_race_recovery_and_evidence_contract():
    template = _read(TEMPLATES / "library_review.html")
    script = _read(JS / "pages/library_review.js")
    assert "全选（至多 50）" in template and "清空" in template
    assert "复制" in template and "打印" in template
    assert "q.document_id && q.chunk_id" in template
    assert "'/documents/' + q.document_id + '/source?from='" in template
    assert "this.selectedChapters.length >= 50" in script
    assert "this.chapters.slice(0, 50)" in script
    assert "request !== this.outlineRequest" in script
    assert "request !== this.quizRequest" in script
    assert "request !== this.chapterRequest" in script
    assert "Array.isArray(data.questions)" in script


def test_source_back_search_and_reduced_motion_contract():
    template = _read(TEMPLATES / "document_source.html")
    script = _read(JS / "pages/document_source.js")
    assert 'id="source-back"' in template
    assert 'id="source-search-input"' in template
    assert 'id="source-search-status" role="status"' in template
    assert "safeInternalPath" in script and "document.referrer" in script
    assert "document.createTreeWalker" in script
    assert "document.createElement('mark')" in script
    assert "prefers-reduced-motion: reduce" in script
    assert "behavior: reducedMotion() ? 'auto' : 'smooth'" in script


def test_library_template_renders_parse_error_and_polling_anchor():
    course = make_course(7, scope="personal", owner_id=1)
    failed = make_doc(11, owner_id=1, course_id=7, status="failed", parse_error="无法提取文本，请检查文件")
    parsing = make_doc(12, owner_id=1, course_id=7, status="parsing", file_name="解析中.pdf")
    session = FakeSession(results=[
        FakeResult(rows=[]),
        FakeResult(rows=[course]),
        FakeResult(rows=[]),
        FakeResult(rows=[failed, parsing]),
        FakeResult(rows=[]),
    ])
    with http_client(session, USER) as client:
        resp = client.get("/library")
    assert resp.status_code == 200
    assert "无法提取文本，请检查文件" in resp.text
    assert 'data-document-id="12" data-course-id="7"' in resp.text
    assert 'data-status="parsing"' in resp.text


def test_chat_review_and_source_render_learning_controls():
    gets = {(Course, 7): make_course(7, scope="personal", owner_id=1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.get("/library/chat", params={"course_id": 7})
    assert resp.status_code == 200
    assert "当前课程：操作系统" in resp.text
    assert "今日剩余" not in resp.text  # 额度必须由客户端真实接口补载，不伪造 SSR 数字
    assert '"course_name": "操作系统"' in resp.text

    with http_client(user=USER) as client:
        resp = client.get("/library/review")
    assert resp.status_code == 200
    assert "全选（至多 50）" in resp.text
    assert "q.document_id && q.chunk_id" in resp.text

    doc = make_doc(11, owner_id=1)
    session = FakeSession(
        gets={(Document, 11): doc, (Course, 5): make_course(scope="personal", owner_id=1)},
        results=[FakeResult(rows=[make_chunk(document_id=11)])],
    )
    with http_client(session, USER) as client:
        resp = client.get("/documents/11/source?from=/library/chat%3Fcourse_id%3D7")
    assert resp.status_code == 200
    assert "在原文中搜索" in resp.text
    assert "上一处" in resp.text and "下一处" in resp.text


def test_owned_templates_keep_csp_no_inline_script_or_style():
    names = (
        "base.html", "login.html", "index.html", "library.html", "library_chat.html",
        "library_review.html", "document_source.html", "_icons.html", "_illustrations.html",
        "_page_header.html", "_precheck.html",
    )
    for name in names:
        text = _read(TEMPLATES / name)
        assert "style=" not in text, name
        for line in text.splitlines():
            if "<script" in line:
                assert 'src="/static/' in line or 'type="application/json"' in line, (name, line)
