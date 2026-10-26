"""v1.4 真实浏览器质量门槛；在 Playwright 官方容器内运行，不访问宿主机浏览器。"""

import json
import os
import re
import time
from pathlib import Path

import pytest

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
expect = pytest.importorskip("playwright.sync_api").expect

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "tests" / "artifacts" / "v14"
DOCS_SHOTS = ROOT / "docs" / "images" / "v14"
AXE = (ROOT / "tests" / "fixtures" / "browser" / "axe-4.10.3.min.js").read_text(encoding="utf-8")
AXE_URL = "/__v14_axe__.js"  # 同源路径，由 Playwright 拦截返回，避免 CSP 拦截内联注入
BASE = os.environ.get("V14_BASE_URL", "http://host.docker.internal:8001")
ACCOUNTS = {
    "student": ("20260001", "zhangsan@demo.stu.example.edu.cn"),
    "reviewer": ("20260101", "lisi@demo.stu.example.edu.cn"),
    "builder": ("20260201", "zhaoliu@demo.stu.example.edu.cn"),
    "admin": ("20230001", "admin@stu.example.edu.cn"),
}
VIEWPORTS = ((360, 800), (390, 844), (768, 1024), (1280, 900), (1440, 960))


def shot(page, name):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(ARTIFACTS / f"{name}.png"), full_page=True)


def docs_shot(page, name):
    DOCS_SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(DOCS_SHOTS / f"{name}.png"), full_page=True)


_SESSION_COOKIES = {}


def login(page, role):
    """API 登录并按角色缓存 Cookie；同一轮测试内复用，避免触发 nginx 认证限流。"""
    cached = _SESSION_COOKIES.get(role)
    if cached is not None:
        page.context.add_cookies(cached)
        page.goto(BASE + "/", wait_until="domcontentloaded")
        return
    student_no, email = ACCOUNTS[role]
    payload = {}
    for attempt in range(2):
        response = page.request.post(
            BASE + "/api/auth/email/code",
            data={"student_no": student_no, "email": email},
        )
        payload = response.json()
        if response.status != 429:
            break
        match = re.search(r"(\d+) 秒", payload.get("detail", ""))
        if not match:
            raise AssertionError(
                "发码被 IP 维度限流（30 次/小时）；请先清 Redis "
                "knowisle:email_send_ip:* 后重跑：" + str(payload)
            ) from None
        assert attempt == 0, payload
        page.wait_for_timeout((int(match.group(1)) + 1) * 1000)
    assert "dev_code" in payload, payload
    verified = page.request.post(
        BASE + "/api/auth/email/verify",
        data={"student_no": student_no, "email": email, "code": payload["dev_code"]},
    )
    assert verified.ok, verified.text()
    body = verified.json()
    assert not body.get("need_profile"), body
    _SESSION_COOKIES[role] = page.context.cookies()
    page.goto(BASE + "/", wait_until="domcontentloaded")


def json_get(page, path):
    return page.evaluate("""async (path) => {
      const response = await fetch(new URL(path, location.origin));
      if (!response.ok) throw new Error(path + ': ' + response.status);
      return response.json();
    }""", path)


def upload_and_wait(page, fixture):
    """上传后以接口返回的 document_id 轮询状态，等待解析完成且 chunks 已提交。"""
    with page.expect_response(
        lambda r: r.request.method == "POST" and re.search(r"/api/documents/?$", r.url),
        timeout=60000,
    ) as response_info:
        page.locator('[data-slot="upload-button"], button:has-text("上传")').last.click()
    response = response_info.value
    assert response.status < 400, response.text()
    doc_id = int(response.json()["document_id"])
    status = {}
    for _ in range(120):
        status = json_get(page, f"/api/documents/{doc_id}/status")
        if status.get("status") == "failed":
            raise AssertionError(f"文档解析失败：{status}")
        if status.get("status") == "parsed" and status.get("chunk_count", 0) > 0:
            break
        page.wait_for_timeout(500)
    else:
        raise AssertionError(f"文档未就绪：{status}")
    # 解析 worker 在 commit 后失效 BM25 缓存；留出跨进程版本戳生效的余量
    page.wait_for_timeout(600)
    return doc_id


def expect_chat_answer(page):
    try:
        expect(page.locator(".md-body:visible").first).to_be_visible(timeout=90000)
    except AssertionError:
        alerts = page.locator('[x-ref="msgBox"] [role="alert"]:visible').all_text_contents()
        body = page.locator("body").inner_text()
        raise AssertionError(f"AI 回答未完成；页面错误={alerts}；正文={body}") from None


def discover(page, role):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    majors = json_get(page, "/api/majors")["items"]
    courses = json_get(page, "/api/courses?size=100")["items"]
    resources = json_get(page, "/api/resources?size=50")["items"] if role != "anonymous" else []
    posts = json_get(page, "/api/posts?size=100")["items"]
    major_id = majors[0]["id"] if majors else 1
    course_id = courses[0]["id"] if courses else 1
    resource_id = resources[0]["id"] if resources else None
    post_id = posts[0]["id"] if posts else 1
    source_id = None
    # 资源可能指向个人库文档（历史 E2E 数据），只有当前角色可读的溯源页才纳入路由，
    # 避免把权限 404 误判为控制台错误。
    for item in resources:
        page.goto(f"{BASE}/resources/{item['id']}", wait_until="domcontentloaded")
        link = page.locator('a[href^="/documents/"]').first.get_attribute("href")
        if not link:
            continue
        candidate = int(re.search(r"/documents/(\d+)/source", link).group(1))
        if page.request.get(f"{BASE}/documents/{candidate}/source").status == 200:
            resource_id = item["id"]
            source_id = candidate
            break
    return {
        "index": "/",
        "login": "/login",
        "404": "/no-such-page-v14",
        "majors": "/majors",
        "major_detail": f"/majors/{major_id}",
        "course_detail": f"/courses/{course_id}",
        "resource_detail": f"/resources/{resource_id}" if resource_id else None,
        "document_source": f"/documents/{source_id}/source" if source_id else None,
        "library": "/library",
        "library_chat": f"/library/chat?course_id={course_id}",
        "library_review": f"/library/review?course_id={course_id}",
        "board": "/boards/qa",
        "post_new": "/posts/new?board=qa",
        "post_detail": f"/posts/{post_id}",
        "search": "/search",
        "me": "/me",
        "review": "/review",
        "admin_review": "/admin/review",
        "admin": "/admin",
    }


def assert_layout(page, name):
    h1 = page.locator("h1")
    expect(h1).to_have_count(1)
    expect(h1.first).to_be_visible()
    overflow = page.evaluate("""() => {
      const root = document.documentElement;
      return {scroll: root.scrollWidth, client: root.clientWidth,
              offenders: [...document.querySelectorAll('body *')].filter(el => {
                if (el.closest('.pointer-events-none')) return false;
                const rect = el.getBoundingClientRect();
                return rect.width && (rect.right > root.clientWidth + 2 || rect.left < -2);
              }).slice(0, 5).map(el => el.tagName + '.' + el.className)};
    }""")
    assert overflow["scroll"] <= overflow["client"] + 2, (name, overflow)
    assert not overflow["offenders"], (name, overflow["offenders"])


def axe_violations(page):
    page.route("**" + AXE_URL, lambda route: route.fulfill(
        status=200, content_type="application/javascript", body=AXE))
    try:
        page.add_script_tag(url=BASE + AXE_URL)
        result = page.evaluate("""async () => {
          const tags = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'];
          const report = await axe.run(document, {runOnly: {type: 'tag', values: tags}});
          return report.violations.filter(v => ['critical', 'serious'].includes(v.impact))
            .map(v => ({id: v.id, impact: v.impact, description: v.description,
                        targets: v.nodes.slice(0, 3).map(n => n.target.join(' '))}));
        }""")
    finally:
        page.unroute("**" + AXE_URL)
    assert result == [], json.dumps(result, ensure_ascii=False, indent=2)


def page_routes(role):
    return {
        "index": True, "login": True, "404": True, "majors": True,
        "major_detail": True, "course_detail": True, "resource_detail": role != "anonymous",
        "document_source": role != "anonymous", "library": role != "anonymous",
        "library_chat": role != "anonymous", "library_review": role != "anonymous",
        "board": True, "post_new": role != "anonymous", "post_detail": True,
        "search": role != "anonymous", "me": role != "anonymous",
        "review": role in {"reviewer", "admin"}, "admin_review": role == "admin",
        "admin": role == "admin",
    }


def test_v14_all_pages_viewports_axe_console_and_assets():
    errors = []
    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        for role in ("anonymous", "student", "reviewer", "builder", "admin"):
            context = browser.new_context(
                viewport={"width": 1280, "height": 900}, ignore_https_errors=True
            )
            page = context.new_page()

            def on_console(msg, page=page):
                expected_404 = msg.location.get("url") == BASE + "/no-such-page-v14"
                # 登录发码的 60s 冷却与 IP 限流是预期 429（login() 会退避重试），不作为控制台错误。
                expected_429 = "/api/auth/email/code" in msg.text and "429" in msg.text
                if msg.type == "error" and not expected_404 and not expected_429:
                    errors.append(f"console:{page.url}:{msg.text}@{msg.location.get('url')}")

            def on_page_error(exc, page=page):
                errors.append(f"pageerror:{page.url}:{exc}")

            def on_request(req):
                local = req.url.startswith((BASE, "data:", "blob:"))
                if not local:
                    errors.append(f"external:{req.url}")

            page.on("console", on_console)
            page.on("pageerror", on_page_error)
            page.on("request", on_request)
            if role != "anonymous":
                login(page, role)
            routes = {k: v for k, v in discover(page, role).items() if v and page_routes(role)[k]}
            for name, path in routes.items():
                for width, height in VIEWPORTS:
                    try:
                        page.set_viewport_size({"width": width, "height": height})
                        response = page.goto(BASE + path, wait_until="networkidle")
                        status = response.status if response else None
                        assert response and status < 500, (role, name, status)
                        if os.environ.get("V14_REQUIRE_CSP"):
                            csp = response.headers.get("content-security-policy", "")
                            assert "default-src 'self'" in csp, csp
                            assert "script-src 'self' 'unsafe-eval'" in csp, csp
                        assert_layout(page, f"{role}:{name}:{width}")
                        if width in (390, 1280):
                            axe_violations(page)
                            shot(page, f"{role}-{name}-{width}")
                    except Exception as exc:
                        failures.append(f"{role}:{name}:{width}\n{exc}")
                if role == "student" and name == "index":
                    page.set_viewport_size({"width": 1440, "height": 960})
                    docs_shot(page, "home-desktop")
                if role == "student" and name == "library_chat":
                    page.set_viewport_size({"width": 390, "height": 844})
                    docs_shot(page, "chat-mobile")
                if role == "admin" and name == "admin":
                    page.set_viewport_size({"width": 1440, "height": 960})
                    docs_shot(page, "admin-desktop")
            context.close()
        browser.close()
    problems = errors + failures
    assert problems == [], "\n\n".join(problems)


def test_v14_keyboard_zoom_forced_colors_and_reduced_motion():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
        page = context.new_page()
        login(page, "student")
        page.goto(BASE + "/", wait_until="networkidle")
        assert page.locator('script[src*="motion-10.18.0"]').count() == 0
        page.keyboard.press("Tab")
        active_tag = page.evaluate("document.activeElement && document.activeElement.tagName")
        assert active_tag in {"A", "BUTTON", "INPUT"}
        page.evaluate("document.body.style.zoom = '200%'")
        assert_layout(page, "home-zoom-200")
        page.goto(BASE + "/library", wait_until="networkidle")
        assert_layout(page, "library-zoom-200")
        page.emulate_media(forced_colors="active")
        assert_layout(page, "library-forced-colors")
        browser.close()


def test_v14_key_user_journeys():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        if os.environ.get("V14_DEBUG"):
            page.on("console", lambda msg: print(f"BROWSER:{msg.type}:{msg.text}", flush=True))
            page.on("pageerror", lambda exc: print(f"BROWSER_ERROR:{exc}", flush=True))
        login(page, "student")

        page.goto(BASE + "/majors", wait_until="networkidle")
        page.locator("#major-filter").fill("不存在专业-v14")
        expect(page.get_by_text("没有匹配的专业")).to_be_visible()
        page.get_by_role("button", name="清空筛选").click()
        expect(page.locator("[data-major-card]").first).to_be_visible()

        public_course_id = json_get(page, "/api/courses?size=100")["items"][0]["id"]
        page.goto(BASE + f"/courses/{public_course_id}", wait_until="networkidle")
        if page.get_by_role("tab", name="AI 问答").count():
            page.get_by_role("tab", name="AI 问答").click()
            assert "tab=chat" in page.url
            page.get_by_role("tab", name="资料库").click()
        query_url = f"/courses/{public_course_id}?tab=resources&q=v14-no-match"
        page.goto(BASE + query_url, wait_until="networkidle")
        expect(page.get_by_text("没有匹配的资料，可调整关键词或章节筛选")).to_be_visible()
        if page.get_by_role("button", name="清空筛选，查看全课程").count():
            page.get_by_role("button", name="清空筛选，查看全课程").click()

        page.goto(BASE + "/posts/new?board=qa", wait_until="networkidle")
        page.locator("#post-title").fill("v1.4 草稿恢复检查")
        page.locator("#post-content").fill("刷新后保留这段 Markdown 草稿。")
        page.reload(wait_until="networkidle")
        expect(page.locator("#post-title")).to_have_value("v1.4 草稿恢复检查")
        expect(page.locator("#post-content")).to_have_value("刷新后保留这段 Markdown 草稿。")
        page.get_by_role("button", name="Markdown 预览").click()
        expect(page.locator(".md-body")).to_contain_text("Markdown 草稿")

        course_name = f"v1.4 浏览器验证课程 {time.time_ns()}"
        page.goto(BASE + "/library", wait_until="networkidle")
        page.get_by_role("button", name="新建课程", exact=True).click()
        page.locator("#new-course").fill(course_name)
        page.get_by_role("button", name="创建", exact=True).click()
        expect(page.get_by_text(course_name, exact=True)).to_be_visible()
        upload = page.locator('[data-upload-course]').last
        course_id = int(upload.get_attribute("data-upload-course"))
        upload.click()
        fixture = ARTIFACTS / "v14-upload.md"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(
            f"<!-- {course_name} -->\n# 进程管理\n\n"
            "进程是资源分配与保护的基本单位，线程是 CPU 调度的基本单位。",
            encoding="utf-8",
        )
        page.locator(f"#upload-file-{course_id}").set_input_files(str(fixture))
        upload_and_wait(page, fixture)
        expect(page.locator("body")).to_contain_text("原文溯源")
        page.goto(BASE + f"/library/chat?course_id={course_id}", wait_until="networkidle")
        page.locator("#chat-question").fill("进程和线程分别是什么单位？")
        page.get_by_role("button", name="发送", exact=True).click()
        expect_chat_answer(page)
        expect(page.locator("body")).to_contain_text("本次消耗", timeout=90000)
        expect(page.locator("body")).to_contain_text("引用来源")
        first_citation = page.locator('a[href*="/documents/"][href*="/source"]').first
        with context.expect_page() as source_info:
            first_citation.click()
        page = source_info.value
        page.wait_for_load_state("domcontentloaded")
        expect(page.locator(".chunk-card").first).to_be_visible()

        page.set_viewport_size({"width": 390, "height": 844})
        assert_layout(page, "document-source-mobile")
        page.goto(BASE + "/library", wait_until="networkidle")
        page.locator(f'[data-upload-course="{course_id}"]').click()
        page.locator(f"#upload-file-{course_id}").set_input_files(str(fixture))
        upload_and_wait(page, fixture)
        page.goto(BASE + f"/library/chat?course_id={course_id}", wait_until="networkidle")
        assert_layout(page, "library-chat-mobile")
        page.locator("#chat-question").fill("请再次说明进程和线程的单位。")
        page.get_by_role("button", name="发送", exact=True).click()
        expect_chat_answer(page)
        expect(page.locator("body")).to_contain_text("引用来源", timeout=90000)
        page.get_by_role("button", name="打开导航菜单").click()
        expect(page.locator("#mobile-navigation")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#mobile-navigation")).to_be_hidden()

        login(page, "reviewer")
        page.goto(BASE + "/review", wait_until="networkidle")
        assert_layout(page, "review-workbench-mobile")
        if page.locator('a[href^="/review?task="]').count():
            page.locator('a[href^="/review?task="]').first.click()
            expect(page.get_by_role("button", name="通过", exact=True)).to_be_visible()

        login(page, "admin")
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(BASE + "/admin?tab=dashboard", wait_until="networkidle")
        expect(page.get_by_text("近 7 天趋势", exact=True)).to_be_visible()
        expect(page.get_by_text("查看趋势数据表", exact=True)).to_be_visible()
        expect(page.locator('div[role="img"] canvas').first).to_be_visible(timeout=15000)
        page.get_by_role("tab", name="空间管理").click()
        assert "tab=space" in page.url
        page.get_by_role("tab", name="用户治理").click()
        assert "tab=users" in page.url
        page.goto(BASE + "/admin/review", wait_until="networkidle")
        if page.get_by_role("button", name="展开并加载预览").count():
            page.get_by_role("button", name="展开并加载预览").first.click()
            expect(page.locator("iframe").first).to_be_visible()
        docs_shot(page, "review-desktop")
        browser.close()
