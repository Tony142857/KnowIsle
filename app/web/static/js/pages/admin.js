/* 管理后台交互（admin.html，v1.0 CSP 外置）。
 * adminApi/adminPost —— 统一 fetch 封装；majorImport / adminCourses / createCourse /
 * adminUsers / userRow / adminConfig / adminReports / adminDashboard —— 六个 Tab 的数据源。
 * 注意：本文件须在 Alpine 启动前执行（模板 head 块内同步加载，勿加 defer）。 */
(function () {
  'use strict';

  // 管理后台统一 fetch 封装：204 视为成功空响应，错误抛出 detail
  async function adminApi(url, options) {
    const resp = await fetch(url, options);
    const data = resp.status === 204 ? {} : await resp.json().catch(() => ({}));
    if (!resp.ok && resp.status !== 204) {
      const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      throw new Error(detail || ('请求失败 ' + resp.status));
    }
    return data;
  }
  function adminPost(url, method, body) {
    return adminApi(url, {
      method: method,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  }

  // 专业批量导入：优先按 JSON 解析，失败按逐行「名称,代码」解析
  function majorImport() {
    return {
      text: '', loading: false, error: '', result: '',
      parseItems() {
        const raw = this.text.trim();
        if (!raw) throw new Error('请输入专业名单');
        if (raw.startsWith('[')) {
          const arr = JSON.parse(raw);
          return arr.map(it => ({name: String(it.name || '').trim(),
                                code: it.code ? String(it.code).trim() : null}));
        }
        return raw.split('\n').map(line => line.trim()).filter(Boolean).map(line => {
          const parts = line.split(/[,，]/);
          return {name: parts[0].trim(), code: parts[1] ? parts[1].trim() : null};
        });
      },
      async submit() {
        this.loading = true; this.error = ''; this.result = '';
        try {
          const items = this.parseItems();
          if (!items.length || items.some(it => !it.name)) throw new Error('存在空名称行，请检查格式');
          const data = await adminPost('/api/admin/majors', 'POST', {items: items});
          this.result = '导入完成：新增 ' + data.created + ' 个，跳过已存在 ' + data.skipped + ' 个';
          this.text = '';
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  // 公共课程列表：状态过滤 + 审批通过 / 停用
  function adminCourses() {
    return {
      status: '', items: [], total: 0, loading: false, loaded: false, error: '',
      statusLabel(s) { return {active: '已开通', pending: '待审批', disabled: '已停用'}[s] || s; },
      async load() {
        this.loading = true; this.error = '';
        try {
          const data = await adminApi('/api/admin/courses?size=100'
            + (this.status ? '&status=' + this.status : ''));
          this.items = data.items || [];
          this.total = data.total || 0;
          this.loaded = true;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
      setStatus(c, status) {
        if (status === 'disabled') {
          // 停用为破坏性操作：全局 confirmModal 确认后再提交
          this.$dispatch('zy-ask', {
            title: '停用课程',
            message: '确认停用课程「' + c.name + '」？停用后不再公开展示。',
            confirmText: '停用',
            onConfirm: () => this.applyStatus(c, status),
          });
          return;
        }
        this.applyStatus(c, status);
      },
      async applyStatus(c, status) {
        this.loading = true; this.error = '';
        try {
          await adminPost('/api/admin/courses/' + c.id, 'PATCH', {status: status});
          c.status = status;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  // 创建课程：管理员直接 active；专业列表取自 GET /api/majors
  function createCourse() {
    return {
      majors: [], form: {name: '', major_id: '', description: ''},
      loading: false, error: '', result: '',
      async loadMajors() {
        try {
          const data = await adminApi('/api/majors');
          this.majors = data.items || [];
        } catch (e) { this.error = e.message; }
      },
      async submit() {
        this.error = ''; this.result = '';
        if (!this.form.name.trim()) { this.error = '请填写课程名'; return; }
        if (!this.form.major_id) { this.error = '请选择所属专业'; return; }
        this.loading = true;
        try {
          const data = await adminPost('/api/courses', 'POST', {
            name: this.form.name.trim(),
            major_id: Number(this.form.major_id),
            description: this.form.description.trim() || null,
          });
          this.result = '已创建并开通：' + data.name;
          this.form = {name: '', major_id: '', description: ''};
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  // 用户治理：搜索 + 分页首屏
  function adminUsers() {
    return {
      q: '', items: [], total: 0, loading: false, loaded: false, error: '',
      async load() {
        this.loading = true; this.error = '';
        try {
          const data = await adminApi('/api/admin/users?size=50'
            + (this.q.trim() ? '&q=' + encodeURIComponent(this.q.trim()) : ''));
          this.items = data.items || [];
          this.total = data.total || 0;
          this.loaded = true;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  // 单行用户：角色任命 / 信用裁决 / 信用明细（展开行内操作）
  function userRow(u) {
    return {
      u: u, open: false, role: u.role, delta: null, reason: '',
      logs: null, logsLoading: false,
      loading: false, error: '', msg: '',
      roleLabel(r) { return {student: '学生', reviewer: '协审员', builder: '共建者', admin: '管理员'}[r] || r; },
      async setRole() {
        if (this.role === this.u.role) { this.error = '角色未变更'; this.msg = ''; return; }
        this.loading = true; this.error = ''; this.msg = '';
        try {
          const data = await adminPost('/api/admin/users/' + this.u.id + '/role', 'PUT', {role: this.role});
          this.u.role = data.role;
          this.msg = '已任命为「' + this.roleLabel(data.role) + '」';
        } catch (e) { this.error = e.message; this.role = this.u.role; }
        finally { this.loading = false; }
      },
      async submitCredit() {
        this.error = ''; this.msg = '';
        if (!Number.isInteger(this.delta) || this.delta === 0) { this.error = '分值须为 ±1~100 的非零整数'; return; }
        if (!this.reason.trim()) { this.error = '请填写裁决理由'; return; }
        this.loading = true;
        try {
          const data = await adminPost('/api/admin/users/' + this.u.id + '/credit', 'POST', {
            delta: this.delta, reason: this.reason.trim(),
          });
          this.u.credit = data.credit;
          this.msg = '信用裁决已提交：' + (data.delta >= 0 ? '+' : '') + data.delta
            + '，当前信用分 ' + data.credit;
          this.delta = null; this.reason = '';
          if (this.logs !== null) await this.loadLogs();
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
      async loadLogs() {
        this.logsLoading = true; this.error = '';
        try {
          const data = await adminApi('/api/admin/users/' + this.u.id + '/credit-logs');
          this.logs = data.items || [];
        } catch (e) { this.error = e.message; }
        finally { this.logsLoading = false; }
      },
    };
  }

  // 平台配置：行内编辑生效值 + 保存（PUT），422 detail 行内展示
  function adminConfig() {
    return {
      items: [], drafts: {}, rowErrors: {}, rowMsgs: {},
      loading: false, error: '',
      async load() {
        this.loading = true; this.error = '';
        try {
          const data = await adminApi('/api/admin/config');
          this.items = data.items || [];
          this.drafts = {};
          for (const item of this.items) this.drafts[item.key] = item.value;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
      async save(item) {
        this.loading = true;
        this.rowErrors = {...this.rowErrors, [item.key]: ''};
        this.rowMsgs = {...this.rowMsgs, [item.key]: ''};
        try {
          const data = await adminPost('/api/admin/config/' + item.key, 'PUT',
                                       {value: Number(this.drafts[item.key])});
          this.rowMsgs = {...this.rowMsgs,
                          [item.key]: '已保存：' + data.previous + ' → ' + data.value};
          await this.load();
        } catch (e) {
          this.rowErrors = {...this.rowErrors, [item.key]: e.message};
        } finally { this.loading = false; }
      },
    };
  }

  // 举报处理（v0.8）：GET /api/admin/reports?status=&page=&size=
  // 处理流转：POST /api/admin/reports/{id}/handle {action: processing|resolve|dismiss, note?}
  function adminReports() {
    return {
      status: '', items: [], total: 0, page: 1, size: 20,
      loading: false, loaded: false, error: '', msg: '',
      statusLabel(s) {
        return {open: '待处理', processing: '处理中', resolved: '已解决', dismissed: '已驳回'}[s] || s;
      },
      targetLabel(t) {
        return {resource: '资源', post: '帖子', comment: '评论', user: '用户'}[t] || t;
      },
      // 评论无法定位到楼层、用户无公开主页：这两类仅显示摘要文本
      targetLink(r) {
        if (r.target_type === 'resource') return '/resources/' + r.target_id;
        if (r.target_type === 'post') return '/posts/' + r.target_id;
        return null;
      },
      async load(page) {
        this.page = Math.max(1, page);
        this.loading = true; this.error = ''; this.msg = '';
        try {
          let url = '/api/admin/reports?page=' + this.page + '&size=' + this.size;
          if (this.status) url += '&status=' + this.status;
          const data = await adminApi(url);
          this.items = data.items || [];
          this.total = data.total || 0;
          this.loaded = true;
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
      handle(r, action) {
        // 解决 / 驳回可附备注：全局 confirmModal 输入（选填）；开始处理直接提交
        if (action === 'processing') {
          this.submitHandle(r, action, null);
          return;
        }
        this.$dispatch('zy-ask', {
          title: action === 'resolve' ? '解决举报' : '驳回举报',
          withInput: true,
          placeholder: action === 'resolve' ? '处理备注（选填）' : '驳回理由（选填）',
          confirmText: action === 'resolve' ? '解决' : '驳回',
          onConfirm: (note) => this.submitHandle(r, action, note),
        });
      },
      async submitHandle(r, action, note) {
        this.loading = true; this.error = ''; this.msg = '';
        try {
          const body = {action: action};
          if (note && note.trim()) body.note = note.trim();
          await adminPost('/api/admin/reports/' + r.id + '/handle', 'POST', body);
          this.msg = '已提交处理结果';
          await this.load(this.page);
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  // 运营看板（v0.8）：GET /api/admin/dashboard → 指标卡组 + 近 7 天趋势折线
  // 切到该 Tab 时父级派发 admin:dashboard 事件，此处首次 fetch 并初始化图表
  // （x-show 隐藏容器尺寸为 0，ECharts 必须在 Tab 可见后的 nextTick 初始化）
  function adminDashboard() {
    return {
      inited: false, loading: false, error: '', data: null, chart: null,
      onShow() {
        if (this.inited) {
          // 再次切回时容器从隐藏恢复，需重算尺寸
          if (this.chart) this.chart.resize();
          return;
        }
        this.inited = true;
        this.$nextTick(() => this.load());
      },
      async load() {
        this.loading = true; this.error = '';
        try {
          this.data = await adminApi('/api/admin/dashboard');
          this.$nextTick(() => this.renderChart());
        } catch (e) { this.error = e.message; }
        finally { this.loading = false; }
      },
      renderChart() {
        const el = this.$refs.trendChart;
        if (!el || typeof echarts === 'undefined' || !this.data) return;
        if (!this.chart) this.chart = echarts.init(el);
        const t = this.data.trend || {};
        this.chart.setOption({
          tooltip: {trigger: 'axis'},
          legend: {data: ['问答', '发帖', '上传']},
          grid: {left: 48, right: 16, top: 32, bottom: 24},
          xAxis: {type: 'category', data: t.days || []},
          yAxis: {type: 'value', minInterval: 1},
          series: [
            {name: '问答', type: 'line', smooth: true, data: t.qa || []},
            {name: '发帖', type: 'line', smooth: true, data: t.posts || []},
            {name: '上传', type: 'line', smooth: true, data: t.uploads || []},
          ],
        });
      },
    };
  }

  window.adminApi = adminApi;
  window.adminPost = adminPost;
  window.majorImport = majorImport;
  window.adminCourses = adminCourses;
  window.createCourse = createCourse;
  window.adminUsers = adminUsers;
  window.userRow = userRow;
  window.adminConfig = adminConfig;
  window.adminReports = adminReports;
  window.adminDashboard = adminDashboard;
})();
