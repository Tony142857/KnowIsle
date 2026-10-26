const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
function makeContext(url = '/courses/5') {
  const parsed = new URL(url, 'http://local');
  const listeners = {};
  const document = {
    addEventListener(name, fn) { if (name === 'alpine:init') fn(); },
    querySelector() { return null; },
    getElementById() { return null; },
    createElement() { return {classList: {add() {}}, setAttribute() {}, appendChild() {}, click() {}, remove() {}}; },
    body: {appendChild() {}},
  };
  const window = {
    location: {pathname: parsed.pathname, search: parsed.search, href: ''},
    history: {calls: [], pushState(_s, _t, value) { this.calls.push(['push', value]); }, replaceState(_s, _t, value) { this.calls.push(['replace', value]); }},
    document,
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    dispatchEvent() {},
    listeners,
    localStorage: {data: {}, getItem(k) { return this.data[k] || null; }, setItem(k, v) { this.data[k] = v; }, removeItem(k) { delete this.data[k]; }},
    ZY: {
      api: {calls: [], async get(url) { this.calls.push(['GET', url]); return {items: [], total: 0}; }, async post(...args) { this.calls.push(['POST', ...args]); return {}; }, async put() {}, async del() {}},
      md: {render(value) { return '<p>' + value + '</p>'; }, highlight() {}, renderInto() {}},
      toast() {}, relTime() { return ''; }, report() {}, setBusy() {},
    },
    Alpine: {data() {}},  // components.js 在 alpine:init 中注册组件，测试里只需空实现
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options && options.detail; } },
    setTimeout, clearTimeout, setInterval, clearInterval, URL, URLSearchParams, Date, JSON, Math, Number, String, Object, Array, Promise, Error, RegExp, encodeURIComponent, decodeURIComponent, console,
  };
  window.window = window;
  window.globalThis = window;
  return vm.createContext(window);
}
function load(file, context) {
  const source = fs.readFileSync(path.join(root, 'app/web/static/js/pages', file), 'utf8');
  vm.runInContext(source, context, {filename: file});
}
function loadShared(file, context) {
  const source = fs.readFileSync(path.join(root, 'app/web/static/js', file), 'utf8');
  vm.runInContext(source, context, {filename: file});
}

test('majors filters name and code immediately', () => {
  const c = makeContext('/majors');
  load('majors.js', c);
  const page = c.majorFilter();
  page.$root = {querySelectorAll() { return []; }};
  page.cards = [{dataset: {majorName: '计算机 0809'}}, {dataset: {majorName: '数学 0701'}}];
  page.query = '0809';
  assert.equal(page.visibleCount, 1);
  assert.equal(page.matches('计算机 0809'), true);
  assert.equal(page.matches('数学 0701'), false);
});

test('course resources serialize q/chapter/page and restore tab from URL', async () => {
  const c = makeContext('/courses/5?tab=resources&q=cache&chapter_id=8&page=2');
  c.ZY.api.get = async url => { c.ZY.api.calls.push(['GET', url]); return {items: [{id: 1}], total: 20}; };
  load('course_detail.js', c);
  const page = c.courseTabs(5, true);
  page.$watch = () => {};
  page.readUrl();
  await page.loadResources();
  assert.equal(page.page, 2);
  assert.match(c.ZY.api.calls[0][1], /q=cache/);
  assert.match(c.ZY.api.calls[0][1], /chapter_id=8/);
  assert.match(c.ZY.api.calls[0][1], /page=2/);
  page.setTab('rank');
  assert.match(c.history.calls.at(-1)[1], /tab=rank/);
});

test('paid download confirms real balance and insufficient balance stays on page', async () => {
  const c = makeContext('/resources/2');
  load('resource_detail.js', c);
  const page = c.resourceDownload(2, 30, false);
  page.$dispatch = (_name, detail) => { page.confirmation = detail; };
  c.ZY.api.get = async () => ({score: 20});
  await page.requestDownload();
  assert.equal(page.confirmation, undefined);
  assert.match(page.error, /还差 10/);
  c.ZY.api.get = async () => ({score: 50});
  await page.requestDownload();
  assert.match(page.confirmation.message, /下载后余额 20 分/);
  assert.equal(typeof page.confirmation.onConfirm, 'function');
});

test('download HTTP errors remain inline instead of navigating', async () => {
  const c = makeContext('/resources/2');
  c.fetch = async () => ({ok: false, status: 422, json: async () => ({detail: '贡献分不足'})});
  load('resource_detail.js', c);
  const page = c.resourceDownload(2, 0, true);
  await page.download();
  assert.equal(page.error, '贡献分不足');
  assert.equal(c.location.href, '');
  assert.equal(page.loading, false);
});

test('post editor restores preselected draft and only clears it after success', async () => {
  const c = makeContext('/posts/new?board=qa&course_id=5');
  loadShared('api.js', c);  // v1.4 草稿能力统一由 api.js 提供
  c.localStorage.data['zhiyu:draft:v2:7:post-draft'] = JSON.stringify({savedAt: Date.now(), value: {board: 'qa', title: '草稿', content: '**正文**', courseId: '5'}});
  c.ZY.api.get = async url => url === '/api/users/me' ? {score: 100} : {items: url.includes('chapters') ? [] : [{id: 5, name: '操作系统', major_name: '计算机'}]};
  load('post_new.js', c);
  const page = c.postNew('qa', 7);
  page.$watch = () => {};
  await page.init();
  assert.equal(page.title, '草稿');
  assert.equal(page.courseId, '5');
  page.$nextTick = fn => fn(); page.$root = {};
  page.showPreview();
  assert.match(page.previewHtml, /正文/);
  c.ZY.api.post = async () => { throw new Error('发布失败'); };
  await page.doSubmit();
  assert.ok(c.localStorage.data['zhiyu:draft:v2:7:post-draft']);
  c.ZY.api.post = async () => ({post_id: 9});
  await page.doSubmit();
  assert.equal(c.localStorage.data['zhiyu:draft:v2:7:post-draft'], undefined);
});

test('comment acceptance supports QA reassignment and locks settled bounty', () => {
  const c = makeContext('/posts/3');
  c.document.getElementById = id => id === 'post-page-data' ? {textContent: JSON.stringify({board: 'qa', is_author: true, user: {}})} : null;
  load('post_detail.js', c);
  const root = {classList: {remove() {}, add() {}}, querySelector() { return {hidden: true}; }};
  const comment = c.commentBox(8, 0, 0, true, false, true);
  comment.$root = root;
  comment.handleAccepted({commentId: 4});
  assert.equal(comment.canAccept, true);
  comment.handleAccepted({commentId: 8});
  assert.equal(comment.canAccept, false);
  c.document.getElementById = () => ({textContent: JSON.stringify({board: 'bounty', is_author: true, user: {}})});
  const bounty = c.commentBox(8, 0, 0, true, true, true);
  bounty.$root = root;
  bounty.handleAccepted({commentId: 4});
  assert.equal(bounty.canAccept, false);
});

test('new QA self-answer is immediately acceptable but bounty self-response is not', () => {
  const c = makeContext('/posts/3');
  const script = fs.readFileSync(path.join(root, 'app/web/static/js/pages/post_detail.js'), 'utf8');
  vm.runInContext(script.replace('window.aiPoll =', 'window.__newCommentCanAccept = newCommentCanAccept;\n  window.aiPoll ='), c);
  c.document.getElementById = () => ({textContent: JSON.stringify({board: 'qa', is_author: true, user: {}})});
  assert.equal(c.__newCommentCanAccept(), true);
  c.document.getElementById = () => ({textContent: JSON.stringify({board: 'bounty', is_author: true, user: {}})});
  assert.equal(c.__newCommentCanAccept(), false);
  const template = readTemplate('post_detail.html');
  assert.match(template, /post\.board == 'qa' or \(c\.author_id != post\.author_id and not post\.accepted_comment_id\)/);
});

function readTemplate(name) {
  return fs.readFileSync(path.join(root, 'app/web/templates', name), 'utf8');
}

test('AI retry calls recovery endpoint and restarts bounded polling', async () => {
  const c = makeContext('/posts/3');
  load('post_detail.js', c);
  const poll = c.aiPoll(3, 'answer', 'failed');
  c.ZY.api.post = async (url, body) => { c.retried = {url, body}; return {}; };
  await poll.retry();
  assert.equal(c.retried.url, '/api/posts/3/ai-retry');
  assert.equal(c.retried.body.kind, 'answer');
  assert.equal(poll.status, 'pending');
  assert.ok(poll.deadline <= Date.now() + 120000);
  poll.stop();
});

test('me pagination appends and quota exchange confirms and updates score', async () => {
  const c = makeContext('/me');
  load('me.js', c);
  const page = c.mePage(100);
  page.quota = {exchange_rate: 10, bonus_balance: 2};
  page.$dispatch = (_name, detail) => { page.confirmation = detail; };
  page.exchangeCount = 2;
  page.exchange();
  assert.match(page.confirmation.message, /兑换后剩余 80 分/);
  c.ZY.api.post = async () => ({score: 80, spent: 20, bonus_balance: 4});
  c.ZY.api.get = async url => {
    if (url.includes('quota')) return {exchange_rate: 10, bonus_balance: 4};
    return {items: [{id: page.favsPage + 1, target_type: 'post'}], total: 2};
  };
  await page.doExchange(2);
  assert.equal(page.score, 80);
  await page.loadFavs(); await page.loadFavs(true);
  assert.equal(page.favs.length, 2);
});

test('search sends major filter, paginates groups independently and clears chips', async () => {
  let factory;
  const c = makeContext('/search?q=OS&major_id=3');
  c.Alpine = {data(_name, fn) { factory = fn; }};
  c.document.getElementById = id => {
    if (id === 'page-data') return {textContent: JSON.stringify({q: 'OS', type: 'all'})};
    return {focus() {}};
  };
  c.ZY.api.get = async url => {
    c.lastUrl = url;
    return {posts: {items: [{id: 1}], total: 2}, resources: {items: [{id: 2}], total: 1}};
  };
  load('search.js', c);
  const page = factory();
  page.submittedQuery = 'OS';
  await page.runSearch();
  assert.match(c.lastUrl, /major_id=3/);
  await page.loadMore('post');
  assert.match(c.lastUrl, /type=post/);
  assert.match(c.lastUrl, /page=2/);
  assert.equal(page.resourcePage, 1);
  page.clearAll();
  assert.equal(page.majorId, '');
  assert.equal(page.searched, false);
});

test('review preview backs off and verdict requires confirmation', () => {
  const c = makeContext('/review?task=4');
  load('review.js', c);
  const page = c.coVerdict(4, '/api/resources/2/preview');
  page.previewAttempts = 1;
  page.schedulePreview();
  assert.equal(page.previewFailed, true);
  assert.match(page.previewMessage, /2 秒/);
  clearTimeout(page.previewTimer);
  page.comment = 'ok';
  page.$dispatch = (_name, detail) => { page.confirmation = detail; };
  page.submit('approve');
  assert.equal(page.confirmation.title, '确认协审通过');
});

test('admin final preview is lazy and reassignment keeps task row', async () => {
  const c = makeContext('/admin/review');
  load('admin_review.js', c);
  const final = c.finalVerdict(4, '/api/resources/2/preview');
  assert.equal(final.previewUrl, '');
  final.togglePreview();
  assert.match(final.previewUrl, /preview/);
  const state = {textContent: ''};
  const row = {removed: false, querySelector() { return state; }, remove() { this.removed = true; }};
  c.document.getElementById = id => id === 'co-task-4' ? row : null;
  const board = c.coReviewBoard();
  board.reassignTaskId = 4; board.reassignOpen = true;
  c.ZY.api.post = async () => ({assignee_ids: [8, 9], escalated: false});
  await board.confirmReassign();
  assert.equal(row.removed, false);
  assert.match(state.textContent, /2 人（已改派）/);
});

test('admin tabs restore URL and load data only when activated', async () => {
  const c = makeContext('/admin?tab=users');
  load('admin.js', c);
  const tabs = c.adminTabs();
  tabs.$nextTick = fn => fn();
  tabs.init();
  assert.equal(tabs.tab, 'users');
  const courses = c.adminCourses();
  courses.onTab('users');
  assert.equal(c.ZY.api.calls.length, 0);
  c.ZY.api.get = async url => { c.ZY.api.calls.push(['GET', url]); return {items: [], total: 45}; };
  await courses.load(2);
  assert.match(c.ZY.api.calls[0][1], /page=2/);
  assert.match(c.ZY.api.calls[0][1], /size=20/);
});
