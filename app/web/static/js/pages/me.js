(function () {
  'use strict';

  function setUnreadBadge(unread) {
    const badge = document.getElementById('notif-badge');
    if (!badge) return;
    badge.textContent = unread > 99 ? '99+' : unread;
    badge.classList.toggle('hidden', unread === 0);
  }

  function updateUnreadBadge(delta) {
    const badge = document.getElementById('notif-badge');
    const current = badge ? (parseInt(badge.textContent, 10) || 0) : 0;
    setUnreadBadge(Math.max(0, current + delta));
    ZY.api.get('/api/notifications/unread-count')
      .then(data => setUnreadBadge(data.unread || 0)).catch(() => {});
  }

  function mePage(initialScore) {
    return {
      loading: false, score: initialScore,
      logs: [], logsTotal: 0, logsPage: 1, logsLoading: false, logsLoaded: false, logsError: '',
      quota: null, quotaError: '', exchangeCount: 1, exchangeMsg: '', exchangeLoading: false,
      keyInfo: null, keyEditing: false, keyError: '', keyLoading: false,
      notifActionLoading: false, busyFavorite: '', busyFollow: '',
      keyForm: {base_url: '', api_key: '', model_short: '', model_medium: '', model_long: ''},
      notifs: [], notifsTotal: 0, notifsPage: 1, notifsLoading: false, notifsLoaded: false, notifsError: '',
      favs: [], favsTotal: 0, favsPage: 1, favsLoading: false, favsLoaded: false, favsError: '',
      follows: [], followsTotal: 0, followsPage: 1, followsLoading: false, followsLoaded: false, followsError: '',

      REASON_LABELS: {
        upload_approved: '资料审核通过', answer_accepted: '回答被采纳',
        download_cost: '下载资料支出', download_share: '资料下载分成',
        download_reward: '资料被下载奖励', quota_exchange: '兑换 AI 额度',
        post_featured: '经验帖设为精华', bounty_escrow: '发布求援托管悬赏',
        bounty_award: '求援响应获得悬赏', co_review_reward: '完成协审奖励',
      },
      reasonLabel(reason) { return this.REASON_LABELS[reason] || '其他积分变动'; },
      boardLabel(board) {
        return {qa: '知屿问答', discuss: '讨论区', experience: '经验长廊', bounty: '资料求援'}[board] || '其他板块';
      },
      statusLabel(status) {
        return {active: '正常', muted: '禁言中', frozen: '已冻结', pending: '待审核', disabled: '已停用'}[status] || '未知状态';
      },
      get favResources() { return this.favs.filter(f => f.target_type === 'resource'); },
      get favPosts() { return this.favs.filter(f => f.target_type === 'post'); },
      get followCourses() { return this.follows.filter(f => f.target_type === 'course'); },
      get followUsers() { return this.follows.filter(f => f.target_type === 'user'); },

      loadAll() {
        this.loadLogs(); this.loadQuota(); this.loadKey(); this.loadNotifs();
        this.loadFavs(); this.loadFollows();
      },
      async loadLogs(append) {
        if (this.logsLoading) return;
        const page = append ? this.logsPage + 1 : 1;
        this.logsLoading = true; this.logsError = '';
        try {
          const data = await ZY.api.get('/api/users/me/score-logs?page=' + page + '&size=20');
          this.logs = append ? this.logs.concat(data.items || []) : (data.items || []);
          this.logsTotal = data.total || 0; this.logsPage = page; this.logsLoaded = true;
        } catch (e) { this.logsError = e.message; }
        finally { this.logsLoading = false; }
      },
      setScore(score) {
        this.score = score;
        const top = document.getElementById('top-score');
        if (top) top.textContent = score;
      },
      async loadQuota() {
        this.quotaError = '';
        try { this.quota = await ZY.api.get('/api/users/me/quota'); }
        catch (e) { this.quotaError = e.message; }
      },
      exchange() {
        const count = Number(this.exchangeCount);
        if (!Number.isInteger(count) || count < 1 || count > 100 || !this.quota) {
          this.quotaError = '兑换次数须为 1~100 的整数'; return;
        }
        const cost = count * this.quota.exchange_rate;
        this.$dispatch('zy-ask', {
          title: '确认兑换 AI 额度',
          message: '将消耗 ' + cost + ' 贡献分，兑换 ' + count + ' 次 AI 额度；当前贡献分 '
            + this.score + ' 分，兑换后剩余 ' + (this.score - cost) + ' 分。',
          confirmText: '确认兑换', onConfirm: () => this.doExchange(count),
        });
      },
      async doExchange(count) {
        if (this.exchangeLoading) return;
        this.exchangeLoading = true; this.quotaError = ''; this.exchangeMsg = '';
        const before = this.quota ? this.quota.bonus_balance : 0;
        try {
          const data = await ZY.api.post('/api/users/me/quota/exchange', {count: count});
          this.setScore(data.score);
          this.exchangeMsg = '兑换成功：新增 ' + (data.bonus_balance - before) + ' 次额度，消耗 '
            + data.spent + ' 贡献分，剩余 ' + data.score + ' 分。';
          ZY.toast('兑换成功', 'success');
          await this.loadQuota();
        } catch (e) { this.quotaError = e.message; }
        finally { this.exchangeLoading = false; }
      },
      async loadKey() {
        this.keyError = '';
        try { this.keyInfo = await ZY.api.get('/api/users/me/llm-key'); }
        catch (e) { this.keyError = e.message; }
      },
      startEdit() {
        this.keyEditing = true; this.keyError = '';
        this.keyForm = {
          base_url: this.keyInfo.base_url || '', api_key: '',
          model_short: this.keyInfo.model_short || '', model_medium: this.keyInfo.model_medium || '',
          model_long: this.keyInfo.model_long || '',
        };
      },
      async saveKey() {
        this.keyError = '';
        if (!this.keyForm.base_url.trim()) { this.keyError = '请填写 Base URL'; return; }
        if (!this.keyForm.api_key.trim()) { this.keyError = '请填写 API Key'; return; }
        this.keyLoading = true;
        try {
          await ZY.api.put('/api/users/me/llm-key', {
            base_url: this.keyForm.base_url.trim(), api_key: this.keyForm.api_key.trim(),
            model_short: this.keyForm.model_short.trim() || null,
            model_medium: this.keyForm.model_medium.trim() || null,
            model_long: this.keyForm.model_long.trim() || null,
          });
          this.keyEditing = false; this.keyForm.api_key = '';
          ZY.toast('Key 已保存', 'success');
          await this.loadKey(); await this.loadQuota();
        } catch (e) { this.keyError = e.message; }
        finally { this.keyLoading = false; }
      },
      deleteKey() {
        this.$dispatch('zy-ask', {
          title: '删除自定义 Key', message: '确定删除自定义 Key？删除后 AI 问答将回到平台额度通道。',
          confirmText: '删除', danger: true, onConfirm: () => this.doDeleteKey(),
        });
      },
      async doDeleteKey() {
        this.keyLoading = true; this.keyError = '';
        try {
          await ZY.api.del('/api/users/me/llm-key');
          this.keyEditing = false; ZY.toast('已删除自定义 Key', 'success');
          await this.loadKey(); await this.loadQuota();
        } catch (e) { this.keyError = e.message; }
        finally { this.keyLoading = false; }
      },
      async loadNotifs(append) {
        if (this.notifsLoading) return;
        const page = append ? this.notifsPage + 1 : 1;
        this.notifsLoading = true; this.notifsError = '';
        try {
          const data = await ZY.api.get('/api/notifications?page=' + page + '&size=20');
          this.notifs = append ? this.notifs.concat(data.items || []) : (data.items || []);
          this.notifsTotal = data.total || 0; this.notifsPage = page; this.notifsLoaded = true;
        } catch (e) { this.notifsError = e.message; }
        finally { this.notifsLoading = false; }
      },
      async markAllRead() {
        if (this.notifActionLoading) return;
        this.notifActionLoading = true;
        const unread = this.notifs.filter(n => !n.is_read).length;
        try {
          await ZY.api.post('/api/notifications/read', {});
          for (const n of this.notifs) n.is_read = true;
          updateUnreadBadge(-unread);
          ZY.toast('已全部标记为已读', 'success');
        } catch (e) { this.notifsError = e.message; }
        finally { this.notifActionLoading = false; }
      },
      async openNotif(n) {
        if (!n.is_read) {
          try {
            await ZY.api.post('/api/notifications/read', {ids: [n.id]});
            n.is_read = true; updateUnreadBadge(-1);
          } catch (e) { /* 已读失败不阻止打开通知目标 */ }
        }
        if (n.link) window.location.href = n.link;
        else document.getElementById('notification-' + n.id).focus();
      },
      async loadFavs(append) {
        if (this.favsLoading) return;
        const page = append ? this.favsPage + 1 : 1;
        this.favsLoading = true; this.favsError = '';
        try {
          const data = await ZY.api.get('/api/favorites?page=' + page + '&size=20');
          this.favs = append ? this.favs.concat(data.items || []) : (data.items || []);
          this.favsTotal = data.total || 0; this.favsPage = page; this.favsLoaded = true;
        } catch (e) { this.favsError = e.message; }
        finally { this.favsLoading = false; }
      },
      favoriteKey(f) { return f.target_type + ':' + f.target_id; },
      async unfavorite(f) {
        const key = this.favoriteKey(f);
        if (this.busyFavorite) return;
        this.busyFavorite = key; this.favsError = '';
        try {
          await ZY.api.del('/api/favorites', {target_type: f.target_type, target_id: f.target_id});
          this.favs = this.favs.filter(x => x !== f); this.favsTotal = Math.max(0, this.favsTotal - 1);
          ZY.toast('已取消收藏', 'success');
        } catch (e) { this.favsError = e.message; }
        finally { this.busyFavorite = ''; }
      },
      async loadFollows(append) {
        if (this.followsLoading) return;
        const page = append ? this.followsPage + 1 : 1;
        this.followsLoading = true; this.followsError = '';
        try {
          const data = await ZY.api.get('/api/follows?page=' + page + '&size=20');
          this.follows = append ? this.follows.concat(data.items || []) : (data.items || []);
          this.followsTotal = data.total || 0; this.followsPage = page; this.followsLoaded = true;
        } catch (e) { this.followsError = e.message; }
        finally { this.followsLoading = false; }
      },
      followKey(f) { return f.target_type + ':' + f.target_id; },
      async unfollow(f) {
        const key = this.followKey(f);
        if (this.busyFollow) return;
        this.busyFollow = key; this.followsError = '';
        try {
          await ZY.api.del('/api/follows', {target_type: f.target_type, target_id: f.target_id});
          this.follows = this.follows.filter(x => x !== f); this.followsTotal = Math.max(0, this.followsTotal - 1);
          ZY.toast('已取消关注', 'success');
        } catch (e) { this.followsError = e.message; }
        finally { this.busyFollow = ''; }
      },
    };
  }

  window.mePage = mePage;
})();
