/* 个人中心页交互（me.html，v1.0 CSP 外置）。
 * mePage —— 成长看板 / AI 额度 / 自定义 Key / 通知 / 收藏 / 关注 六个区块的统一数据源。
 * 注意：本文件须在 Alpine 启动前执行（模板 head 块内同步加载，勿加 defer）。 */
(function () {
  'use strict';

  function mePage() {
    return {
      loading: false,
      // 积分明细
      logs: [], logsLoading: false, logsLoaded: false, logsError: '',
      // AI 额度
      quota: null, quotaError: '', exchangeCount: 1, exchangeMsg: '',
      // 自定义 Key
      keyInfo: null, keyEditing: false, keyError: '',
      keyForm: {base_url: '', api_key: '', model_short: '', model_medium: '', model_long: ''},
      // 通知
      notifs: [], notifsLoading: false, notifsLoaded: false, notifsError: '',
      // 我的收藏（v0.6）
      favs: [], favsLoading: false, favsLoaded: false, favsError: '',
      // 我的关注（v0.6）
      follows: [], followsLoading: false, followsLoaded: false, followsError: '',

      REASON_LABELS: {
        upload_approved: '资料过审',
        answer_accepted: '回答被采纳',
        download_cost: '下载支出',
        download_share: '下载分成',
        download_reward: '被下载奖励',
        quota_exchange: '额度兑换',
      },
      reasonLabel(r) { return this.REASON_LABELS[r] || r; },
      boardLabel(b) { return {qa: '知屿问答', discuss: '讨论区'}[b] || b; },
      get favResources() { return this.favs.filter(f => f.target_type === 'resource'); },
      get favPosts() { return this.favs.filter(f => f.target_type === 'post'); },
      get followCourses() { return this.follows.filter(f => f.target_type === 'course'); },
      get followUsers() { return this.follows.filter(f => f.target_type === 'user'); },

      loadAll() {
        this.loadLogs(); this.loadQuota(); this.loadKey(); this.loadNotifs();
        this.loadFavs(); this.loadFollows();
      },

      async api(url, options) {
        const resp = await fetch(url, options);
        const data = resp.status === 204 ? {} : await resp.json().catch(() => ({}));
        if (!resp.ok && resp.status !== 204) throw new Error(data.detail || ('请求失败 ' + resp.status));
        return data;
      },

      async loadLogs() {
        this.logsLoading = true; this.logsError = '';
        try {
          const data = await this.api('/api/users/me/score-logs?page=1&size=20');
          this.logs = data.items || [];
          this.logsLoaded = true;
        } catch (e) { this.logsError = e.message; }
        finally { this.logsLoading = false; }
      },

      async loadQuota() {
        this.quotaError = '';
        try {
          this.quota = await this.api('/api/users/me/quota');
        } catch (e) { this.quotaError = e.message; }
      },

      async exchange() {
        if (!(this.exchangeCount >= 1)) { this.quotaError = '兑换次数须 ≥ 1'; return; }
        this.loading = true; this.quotaError = ''; this.exchangeMsg = '';
        try {
          const data = await this.api('/api/users/me/quota/exchange', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({count: this.exchangeCount}),
          });
          this.exchangeMsg = '兑换成功：+' + (data.bonus_balance - (this.quota ? this.quota.bonus_balance : 0)) + ' 次额度';
          await this.loadQuota();
        } catch (e) { this.quotaError = e.message; }
        finally { this.loading = false; }
      },

      async loadKey() {
        this.keyError = '';
        try {
          this.keyInfo = await this.api('/api/users/me/llm-key');
        } catch (e) { this.keyError = e.message; }
      },

      startEdit() {
        this.keyEditing = true; this.keyError = '';
        this.keyForm = {
          base_url: this.keyInfo.base_url || '', api_key: '',
          model_short: this.keyInfo.model_short || '',
          model_medium: this.keyInfo.model_medium || '',
          model_long: this.keyInfo.model_long || '',
        };
      },

      async saveKey() {
        this.keyError = '';
        if (!this.keyForm.base_url.trim()) { this.keyError = '请填写 Base URL'; return; }
        if (!this.keyForm.api_key.trim()) { this.keyError = '请填写 API Key'; return; }
        this.loading = true;
        try {
          await this.api('/api/users/me/llm-key', {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              base_url: this.keyForm.base_url.trim(),
              api_key: this.keyForm.api_key.trim(),
              model_short: this.keyForm.model_short.trim() || null,
              model_medium: this.keyForm.model_medium.trim() || null,
              model_long: this.keyForm.model_long.trim() || null,
            }),
          });
          this.keyEditing = false;
          this.keyForm.api_key = '';
          await this.loadKey();
          await this.loadQuota();
        } catch (e) { this.keyError = e.message; }
        finally { this.loading = false; }
      },

      deleteKey() {
        // 破坏性操作：全局 confirmModal 确认后执行
        this.$dispatch('zy-ask', {
          title: '删除自定义 Key',
          message: '确定删除自定义 Key？删除后 AI 问答将回到平台额度通道。',
          confirmText: '删除',
          onConfirm: () => this.doDeleteKey(),
        });
      },

      async doDeleteKey() {
        this.loading = true; this.keyError = '';
        try {
          await this.api('/api/users/me/llm-key', {method: 'DELETE'});
          this.keyEditing = false;
          await this.loadKey();
          await this.loadQuota();
        } catch (e) { this.keyError = e.message; }
        finally { this.loading = false; }
      },

      async loadNotifs() {
        this.notifsLoading = true; this.notifsError = '';
        try {
          const data = await this.api('/api/notifications?page=1&size=20');
          this.notifs = data.items || [];
          this.notifsLoaded = true;
        } catch (e) { this.notifsError = e.message; }
        finally { this.notifsLoading = false; }
      },

      async markAllRead() {
        this.loading = true;
        try {
          await this.api('/api/notifications/read', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
          });
          for (const n of this.notifs) n.is_read = true;
        } catch (e) { this.notifsError = e.message; }
        finally { this.loading = false; }
      },

      async openNotif(n) {
        if (!n.is_read) {
          try {
            await this.api('/api/notifications/read', {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ids: [n.id]}),
            });
            n.is_read = true;
          } catch (e) { /* 已读标记失败不阻塞跳转 */ }
        }
        if (n.link) window.location.href = n.link;
      },

      async loadFavs() {
        this.favsLoading = true; this.favsError = '';
        try {
          const data = await this.api('/api/favorites?page=1&size=50');
          this.favs = data.items || [];
          this.favsLoaded = true;
        } catch (e) { this.favsError = e.message; }
        finally { this.favsLoading = false; }
      },

      async unfavorite(f) {
        this.loading = true; this.favsError = '';
        try {
          await this.api('/api/favorites', {
            method: 'DELETE', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target_type: f.target_type, target_id: f.target_id}),
          });
          this.favs = this.favs.filter(x => x !== f);
        } catch (e) { this.favsError = e.message; }
        finally { this.loading = false; }
      },

      async loadFollows() {
        this.followsLoading = true; this.followsError = '';
        try {
          const data = await this.api('/api/follows?page=1&size=50');
          this.follows = data.items || [];
          this.followsLoaded = true;
        } catch (e) { this.followsError = e.message; }
        finally { this.followsLoading = false; }
      },

      async unfollow(f) {
        this.loading = true; this.followsError = '';
        try {
          await this.api('/api/follows', {
            method: 'DELETE', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target_type: f.target_type, target_id: f.target_id}),
          });
          this.follows = this.follows.filter(x => x !== f);
        } catch (e) { this.followsError = e.message; }
        finally { this.loading = false; }
      },
    };
  }

  window.mePage = mePage;
})();
