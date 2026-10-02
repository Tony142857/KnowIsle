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
 * - 204 视为成功空响应；其余非 2xx 抛出 Error，消息取自响应体 detail；
 * - ZY.withLoading(ctx, fn)：自动维护 ctx.loading 加载态（配合 :disabled="loading"）。
 */
window.ZY = window.ZY || {};

(function () {
  'use strict';

  async function request(method, url, body) {
    const options = {method: method, headers: {}};
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const resp = await fetch(url, options);
    if (resp.status === 401) {
      window.location.href = '/login';
      throw new Error('登录已过期，请重新登录');
    }
    const data = resp.status === 204 ? {} : await resp.json().catch(() => ({}));
    if (!resp.ok && resp.status !== 204) {
      const detail = typeof data.detail === 'string' ? data.detail
        : (data.detail ? JSON.stringify(data.detail) : '');
      throw new Error(detail || ('请求失败 ' + resp.status));
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
