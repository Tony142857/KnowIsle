/* 知屿 KnowIsle 统一 fetch 封装，挂在 window.ZY 命名空间下。
 *
 * 用法：
 *   const data = await ZY.api.get('/api/courses?size=100');
 *   const data = await ZY.api.post('/api/votes', {target_type: 'post', target_id: 1, value: 1});
 *   const data = await ZY.api.put(url, body) / ZY.api.patch(url, body) / ZY.api.del(url[, body]);
 *
 * 约定：
 * - 自动带 JSON 请求头并序列化 body（body 省略时不带 Content-Type）；
 * - 401 一律跳转 /login（会话过期兜底，各组件另有未登录前置判断）；
 *   例外：/api/auth/* 认证接口的 401 是业务错误（验证码错误等），以及当前已在
 *   登录页时，不跳转——抛错交给页面内联展示（v1.1 修复：登录页误跳导致表单被刷新清空）；
 * - 204 视为成功空响应；其余非 2xx 抛出 Error，消息取自响应体 detail：
 *   - detail 为字符串（403 禁言 / 429 限流 / 业务错误等）原样透传；
 *   - detail 为数组（FastAPI 422 参数校验）拼成可读的字段错误列表；
 *   - 抛出的 Error 带 .status（HTTP 状态码），调用方可按状态码分支处理；
 * - ZY.withLoading(ctx, fn)：自动维护 ctx.loading 加载态（配合 :disabled="loading"）。
 *
 * 全站唯一 fetch 标准（v1.1 F3）：页面不得裸 fetch，统一走本封装。
 */
window.ZY = window.ZY || {};

(function () {
  'use strict';

  var FIELD_LABELS = {
    student_no: '学号', email: '校园邮箱', code: '验证码', real_name: '真实姓名',
    nickname: '昵称', title: '标题', content: '正文', question: '问题', reason: '理由',
    course_id: '课程', chapter_id: '章节', count: '数量', value: '数值', comment: '意见',
  };
  var STATUS_MESSAGES = {
    405: '当前操作方式不受支持，请刷新页面后重试',
    422: '提交内容不符合要求，请检查标出的字段',
    429: '操作过于频繁或额度已用完，请稍后重试',
    500: '服务暂时不可用，请稍后重试',
  };

  function validationMessage(msg) {
    if (!msg) return '字段值无效';
    if (/field required/i.test(msg)) return '此字段必填';
    if (/valid email/i.test(msg)) return '请输入有效邮箱地址';
    if (/valid integer/i.test(msg)) return '请输入整数';
    if (/greater than or equal to/i.test(msg)) return '数值小于允许范围';
    if (/less than or equal to/i.test(msg)) return '数值大于允许范围';
    if (/at most .*characters/i.test(msg)) return '内容超出长度限制';
    if (/at least .*characters/i.test(msg)) return '内容长度不足';
    return msg;
  }

  /* 把 FastAPI 错误体提炼为可读消息 */
  function detailMessage(data, status) {
    var d = data && data.detail;
    if (Array.isArray(d)) {
      var msgs = d.map(function (it) {
        if (it && typeof it === 'object' && it.msg) {
          var parts = Array.isArray(it.loc) ? it.loc.filter(function (p) { return p !== 'body' && p !== 'query'; }) : [];
          var loc = parts.join('.');
          var label = FIELD_LABELS[loc] || loc;
          return (label ? label + '：' : '') + validationMessage(it.msg);
        }
        return typeof it === 'string' ? it : '字段格式无效';
      });
      return msgs.join('；') || STATUS_MESSAGES[422];
    }
    if (status === 405 || status >= 500) return STATUS_MESSAGES[status] || STATUS_MESSAGES[500];
    if (typeof d === 'string' && d) return d;
    return STATUS_MESSAGES[status] || ('请求失败（HTTP ' + status + '）');
  }

  async function request(method, url, body) {
    const options = {method: method, headers: {}};
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const resp = await fetch(url, options);
    if (resp.status === 401) {
      const isAuthApi = url.indexOf('/api/auth/') === 0;
      const onLoginPage = window.location.pathname === '/login';
      if (!isAuthApi && !onLoginPage) {
        window.dispatchEvent(new CustomEvent('zy:session-expired'));
        const next = window.location.pathname + window.location.search + window.location.hash;
        window.location.href = '/login?next=' + encodeURIComponent(next);
        const authError = new Error('登录已过期，未提交内容已保存，请重新登录');
        authError.status = 401;
        throw authError;
      }
    }
    const data = resp.status === 204 ? {} : await resp.json().catch(() => ({}));
    if (!resp.ok && resp.status !== 204) {
      const err = new Error(detailMessage(data, resp.status));
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  window.ZY.api = {
    request: request,
    get: function (url) { return request('GET', url); },
    post: function (url, body) { return request('POST', url, body); },
    put: function (url, body) { return request('PUT', url, body); },
    patch: function (url, body) { return request('PATCH', url, body); },
    del: function (url, body) { return request('DELETE', url, body); },
  };

  window.ZY.drafts = {
    key: function (name, scope) {
      var meta = document.querySelector('meta[name="zy-user-id"]');
      var user = scope || (meta && meta.content) || 'anonymous';
      return 'zhiyu:draft:v2:' + user + ':' + name;
    },
    save: function (name, value, scope) {
      try { localStorage.setItem(this.key(name, scope), JSON.stringify(value)); return true; }
      catch (e) { return false; }
    },
    load: function (name, scope) {
      try {
        var raw = localStorage.getItem(this.key(name, scope));
        return raw ? JSON.parse(raw) : null;
      } catch (e) { return null; }
    },
    remove: function (name, scope) {
      try { localStorage.removeItem(this.key(name, scope)); } catch (e) { /* 存储不可用 */ }
    },
  };

  /* 操作级忙碌态：只标记当前组件，不锁住整页。 */
  window.ZY.setBusy = function (ctx, busy) {
    if (!ctx) return;
    const root = ctx.$root || ctx.$el;
    if (root && root.setAttribute) root.setAttribute('aria-busy', busy ? 'true' : 'false');
  };

  /* 加载态助手：await ZY.withLoading(this, async () => { ... }) */
  window.ZY.withLoading = async function (ctx, fn) {
    ctx.loading = true;
    window.ZY.setBusy(ctx, true);
    try {
      return await fn();
    } finally {
      ctx.loading = false;
      window.ZY.setBusy(ctx, false);
    }
  };
})();
