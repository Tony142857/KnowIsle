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

  /* 把 FastAPI 错误体提炼为可读消息 */
  function detailMessage(data, status) {
    var d = data && data.detail;
    if (d === undefined || d === null || d === '') return '请求失败 ' + status;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
      // 422 校验错误：[{loc: ['body','x'], msg: '...', type: '...'}, ...]
      var msgs = d.map(function (it) {
        if (it && typeof it === 'object' && it.msg) {
          var loc = Array.isArray(it.loc) ? it.loc.filter(function (p) { return p !== 'body'; }).join('.') : '';
          return loc ? (loc + '：' + it.msg) : it.msg;
        }
        return typeof it === 'string' ? it : JSON.stringify(it);
      });
      return msgs.join('；') || ('请求失败 ' + status);
    }
    return JSON.stringify(d);
  }

  async function request(method, url, body) {
    const options = {method: method, headers: {}};
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const resp = await fetch(url, options);
    if (resp.status === 401) {
      // 认证接口的 401 是业务错误（错码/邮箱不符）；登录页自身不跳转，交给页面展示
      const isAuthApi = url.indexOf('/api/auth/') === 0;
      const onLoginPage = window.location.pathname === '/login';
      if (!isAuthApi && !onLoginPage) {
        window.location.href = '/login';
        throw new Error('登录已过期，请重新登录');
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

  /* 加载态助手：await ZY.withLoading(this, async () => { ... }) */
  window.ZY.withLoading = async function (ctx, fn) {
    ctx.loading = true;
    try {
      return await fn();
    } finally {
      ctx.loading = false;
    }
  };
})();
