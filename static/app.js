/* ==========================================================================
   Magent Console — 前端逻辑
   纯原生 JS，无构建步骤、无 CDN 依赖（离线可用）
   ========================================================================== */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  // 环形图周长：2 * PI * r(48)
  const RING_LEN = 301.59;

  // 各等级对应的环填充比例（避免分数绝对值导致环形几乎不可见）
  const GRADE_PCT = { S: 0.95, A: 0.78, B: 0.55, C: 0.32 };

  // 异质分工的角色库：由后端 config/roles.json 提供（可在「设置 → 角色库」里增删改）
  // 启动前若还没拉到，用下面的最小兜底，避免界面空白
  let ROLES = [
    { name: '实现', text: '实现 <功能>：\n- 按现有代码风格，不引入新依赖' },
    { name: '测试', text: '为 <功能> 编写测试：\n- 覆盖正常路径 + 边界 + 异常' },
    { name: 'Review', text: '审查本次改动：\n- 指出 bug / 安全隐患 / 性能问题' },
    { name: '文档', text: '为 <功能> 补充文档：\n- 用法示例 + 参数说明' },
  ];

  // 异质分工下第 i 个 pane 的默认角色名（按角色库循环取）
  function roleDefaultName(i) {
    if (ROLES && ROLES.length) return ROLES[i % ROLES.length].name;
    return 'Agent ' + (i + 1);
  }

  const PARALLEL_TPL =
    '任务：<一句话描述>\n\n背景：\n- <相关上下文>\n\n要求：\n- <约束 1>\n- <约束 2>\n\n验收标准：\n- <可客观判定的标准>\n\n输出：改动文件清单 + 关键决策说明。不要修改与本任务无关的文件。';

  const state = {
    profile: null,
    agents: [],
    sessions: [],
    current: null,      // 当前监控的 session 名
    mode: 'parallel',
    timer: null,
    lastStatus: null,
  };

  // ============================================================ 工具
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function fmtGB(mb) {
    if (mb == null) return '—';
    const gb = mb / 1024;
    return gb >= 1024 ? (gb / 1024).toFixed(1) + ' TB' : gb.toFixed(1) + ' GB';
  }

  function toast(msg, kind) {
    const host = $('snackHost');
    const el = document.createElement('div');
    el.className = 'snackbar' + (kind ? ' snackbar--' + kind : '');
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = 'opacity 200ms, transform 200ms';
      el.style.opacity = '0';
      el.style.transform = 'translateY(8px)';
      setTimeout(() => el.remove(), 220);
    }, kind === 'err' ? 5200 : 3000);
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, opts || {}));
    let data;
    try {
      data = await res.json();
    } catch (e) {
      throw new Error('后端返回非 JSON（HTTP ' + res.status + '）');
    }
    if (!data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data.data;
  }

  // 浏览器上报终端尺寸，供屏幕约束计算
  function screenSize() {
    return {
      cols: Math.floor(window.innerWidth / 8.4),
      rows: Math.floor(window.innerHeight / 20),
    };
  }

  // ============================================================ 性能评估
  async function loadProfile() {
    try {
      const s = screenSize();
      const p = await api('/api/profile?cols=' + s.cols + '&rows=' + s.rows + '&bench=1');
      state.profile = p;
      renderProfile(p);
    } catch (e) {
      toast('性能评估失败：' + e.message, 'err');
    }
  }

  function renderProfile(p) {
    const rec = p.recommendation || {};
    const mem = p.memory || {};
    const cpu = p.cpu || {};
    const gpu = p.gpu || {};
    const disk = p.disk || {};
    const bench = p.cpu_benchmark || {};

    $('perfTime').textContent = p.collected_at || '';

    // 仪表盘
    const grade = rec.grade || 'C';
    const pct = GRADE_PCT[grade] != null ? GRADE_PCT[grade] : 0.32;
    const ring = $('gaugeRing');
    ring.style.strokeDasharray = String(RING_LEN);
    ring.style.strokeDashoffset = String(RING_LEN * (1 - pct));
    $('gaugeGrade').textContent = grade;

    // 推荐数量
    const recommended = rec.recommended || 1;
    const maxCloud = rec.max_agents_cloud || recommended;
    $('recCount').textContent = recommended;
    $('mScore').textContent = (rec.score != null ? rec.score : '—') +
      (bench.score != null ? '  (CPU 基准 ' + bench.score + ')' : '');

    // 本地模型提示
    $('recLocal').textContent = rec.local_model_note || '';

    // 瓶颈
    const bn = $('bottleneck');
    bn.innerHTML = '';
    const bnIcon = document.createElement('span');
    bnIcon.textContent = '⚠';
    const bnText = document.createElement('span');
    bnText.textContent = '瓶颈：' + (rec.bottleneck || '未知') +
      '（云端模式上限 ' + maxCloud + ' 个）';
    bn.appendChild(bnIcon);
    bn.appendChild(bnText);

    // CPU
    $('mCpu').textContent = (cpu.cores || '—') + ' 核';
    $('mCpuSub').textContent = (cpu.model ? String(cpu.model).slice(0, 28) : '') +
      (cpu.threads ? ' · ' + cpu.threads + ' 线程' : '');

    // 内存
    const totalMb = mem.total_mb, availMb = mem.available_mb;
    $('mMem').textContent = fmtGB(availMb) + ' 可用';
    const memPct = totalMb ? Math.round(((totalMb - availMb) / totalMb) * 100) : 0;
    const memBar = $('mMemBar');
    memBar.style.width = memPct + '%';
    memBar.className = 'bar__fill' + (memPct > 85 ? ' bar__fill--danger' : memPct > 70 ? ' bar__fill--warn' : '');
    $('mMemSub').textContent = '共 ' + fmtGB(totalMb) + ' · 已用 ' + memPct + '%';

    // GPU：区分独显 / 核显 / 无
    if (gpu.detected && gpu.vram_mb && !gpu.integrated) {
      $('mGpu').textContent = fmtGB(gpu.vram_mb) + ' 显存';
      const dev0 = (gpu.devices && gpu.devices[0]) || '';
      $('mGpuSub').textContent = (typeof dev0 === 'string' ? dev0 : (dev0.model || '')).slice(0, 30) || '独立显卡';
    } else if (gpu.detected && gpu.integrated) {
      $('mGpu').textContent = '核显';
      const dev0 = (gpu.devices && gpu.devices[0]) || '';
      $('mGpuSub').textContent = ((typeof dev0 === 'string' ? dev0 : (dev0.model || '')) +
        (gpu.vram_is_shared ? ' · 共享内存' : ' · 无独显')).slice(0, 40);
    } else if (gpu.detected) {
      $('mGpu').textContent = '无独显';
      $('mGpuSub').textContent = '本地模型只能 CPU 推理';
    } else {
      $('mGpu').textContent = '无独显';
      $('mGpuSub').textContent = '本地模型只能 CPU 推理';
    }

    // 磁盘（显示项目实际所在磁盘，并标明是哪块盘）
    const freeGb = disk.free_gb || 0;
    $('mDisk').textContent = freeGb.toFixed(1) + ' GB';
    const diskPct = disk.total_gb ? Math.round(((disk.total_gb - freeGb) / disk.total_gb) * 100) : 0;
    const diskBar = $('mDiskBar');
    diskBar.style.width = diskPct + '%';
    diskBar.className = 'bar__fill' + (freeGb < 5 ? ' bar__fill--danger' : diskPct > 90 ? ' bar__fill--warn' : '');
    const diskLabel = disk.drive || disk.mount_point || '';
    $('mDiskSub').textContent = disk.total_gb
      ? (diskLabel ? diskLabel + ' · ' : '') + '共 ' + disk.total_gb.toFixed(0) + ' GB · 已用 ' + diskPct + '%'
      : (diskLabel || '');
    $('mDiskSub').title = disk.path ? ('检测路径：' + disk.path + (disk.device ? '（设备 ' + disk.device + '）' : '')) : '';

    // 理由
    const ul = $('reasonList');
    ul.innerHTML = '';
    (rec.reasons || []).forEach((r) => {
      const li = document.createElement('li');
      li.textContent = r;
      ul.appendChild(li);
    });

    // 同步滑块上限
    state.maxCloud = maxCloud;
    state.recommended = recommended;
    const rng = $('rngCount');
    rng.max = String(Math.max(Math.min(maxCloud + 4, 16), 4));
    if (Number(rng.value) > rec.recommended) rng.value = String(recommended);
    updateCountHint();
    renderTasks(Number(rng.value));
  }

  // ============================================================ Agent 列表
  async function loadAgents() {
    try {
      const d = await api('/api/agents');
      state.agents = d.agents || [];
      renderAgents();
    } catch (e) {
      toast('获取 agent 列表失败：' + e.message, 'err');
    }
  }

  // ============================================================ 角色库
  // 角色库由后端 config/roles.json 托管，可在「设置 → 角色库」里增删改。
  // 拉取失败时保持内存里的兜底角色，不阻塞主流程。
  async function loadRoles() {
    try {
      const d = await api('/api/config');
      const roles = (d && d.roles) || [];
      if (roles.length) ROLES = roles;
    } catch (e) {
      /* 后端不可用时静默沿用兜底角色 */
    }
  }

  function renderAgents() {
    const host = $('agentList');
    host.innerHTML = '';
    state.agents.forEach((a) => {
      const chip = document.createElement('span');
      chip.className = 'chip ' + (a.installed ? 'chip--ok' : '');
      chip.title = a.installed
        ? (a.path || a.cmd || '') + (a.authed ? ' · 已认证' : ' · 未认证')
        : (a.install_hint || '未安装');
      const dot = document.createElement('span');
      dot.className = 'dot';
      chip.appendChild(dot);
      chip.appendChild(document.createTextNode(a.name + (a.installed ? '' : '（未安装）')));
      host.appendChild(chip);
    });

    // 下拉框
    const sel = $('selAgent');
    const prev = sel.value;
    sel.innerHTML = '';
    state.agents.forEach((a) => {
      const opt = document.createElement('option');
      opt.value = a.id;
      opt.textContent = a.name + (a.installed ? '' : '（未安装）') + (a.local ? ' · 本地' : '');
      opt.disabled = !a.installed;
      sel.appendChild(opt);
    });
    if (prev && state.agents.some((a) => a.id === prev && a.installed)) sel.value = prev;
    else {
      const first = state.agents.find((a) => a.installed);
      if (first) sel.value = first.id;
    }
  }

  // ============================================================ 数量滑块
  function updateCountHint() {
    const n = Number($('rngCount').value);
    $('countVal').textContent = String(n);
    const hint = $('countHint');
    const rec = state.recommended || 4;
    const max = state.maxCloud || rec;
    if (n <= rec) {
      hint.textContent = '在推荐范围内（推荐 ' + rec + '）';
      hint.style.color = 'var(--c-text-faint)';
    } else if (n <= max) {
      hint.textContent = '超过推荐值 ' + rec + '，监控压力上升，但硬件仍可承受';
      hint.style.color = 'var(--c-warn)';
    } else {
      hint.textContent = '超出硬件上限 ' + max + '，agent 之间会争抢资源，可能集体变慢';
      hint.style.color = 'var(--c-danger)';
    }
  }

  // ============================================================ 任务列表
  function renderTasks(n) {
    const host = $('taskList');
    const old = {};
    // 保留已输入内容
    host.querySelectorAll('[data-idx]').forEach((el) => {
      const i = el.getAttribute('data-idx');
      const ta = el.querySelector('textarea');
      const nm = el.querySelector('.task-item__name-input');
      old[i] = { name: nm ? nm.value : '', text: ta ? ta.value : '' };
    });

    host.innerHTML = '';
    for (let i = 0; i < n; i++) {
      const item = document.createElement('div');
      item.className = 'task-item';
      item.setAttribute('data-idx', String(i));

      const head = document.createElement('div');
      head.className = 'task-item__head';

      const idx = document.createElement('div');
      idx.className = 'task-item__idx';
      idx.textContent = String(i + 1);

      const nameInput = document.createElement('input');
      nameInput.className = 'input task-item__name-input';
      nameInput.style.cssText = 'height:28px;padding:2px 8px;font-size:12px;max-width:110px';
      nameInput.value = (old[String(i)] && old[String(i)].name) ||
        (state.mode === 'heterogeneous' ? roleDefaultName(i) : 'Agent ' + (i + 1));

      head.appendChild(idx);

      if (state.mode === 'heterogeneous') {
        // 角色下拉：直接选角色库里的角色，选中后自动带出该角色的 requirement 模板
        const sel = document.createElement('select');
        sel.className = 'input task-item__role-select';
        sel.style.cssText = 'height:28px;padding:2px 6px;font-size:12px;max-width:130px';
        const ph = document.createElement('option');
        ph.value = '';
        ph.textContent = '角色…';
        sel.appendChild(ph);
        ROLES.forEach((r) => {
          const op = document.createElement('option');
          op.value = r.name;
          op.textContent = r.name;
          sel.appendChild(op);
        });
        sel.value = ROLES.some((r) => r.name === nameInput.value) ? nameInput.value : '';
        sel.addEventListener('change', () => {
          const r = ROLES.find((x) => x.name === sel.value);
          if (!r) return;
          nameInput.value = r.name;
          const ta2 = item.querySelector('textarea');
          if (ta2 && !ta2.value.trim() && r.text) ta2.value = r.text;
        });
        head.appendChild(sel);
      }

      head.appendChild(nameInput);

      const body = document.createElement('div');
      body.className = 'task-item__body';
      const ta = document.createElement('textarea');
      ta.className = 'textarea';
      ta.placeholder = state.mode === 'parallel'
        ? (i === 0 ? '这份 requirement 会广播给所有 agent' : '并行模式下仅第 1 条会广播')
        : '该 agent 的 requirement';
      ta.value = (old[String(i)] && old[String(i)].text) || '';
      body.appendChild(ta);

      item.appendChild(head);
      item.appendChild(body);
      host.appendChild(item);
    }
  }

  function collectTasks() {
    const out = [];
    document.querySelectorAll('#taskList .task-item').forEach((el) => {
      const nm = el.querySelector('.task-item__name-input');
      const ta = el.querySelector('textarea');
      out.push({ name: nm ? nm.value : '', text: ta ? ta.value : '' });
    });
    return out;
  }

  // ============================================================ 会话 / 监控
  async function loadSessions() {
    try {
      const d = await api('/api/sessions');
      state.sessions = d.sessions || [];
      renderSessions();
      const chip = $('chipSessions');
      chip.textContent = state.sessions.length + ' 会话';
    } catch (e) {
      // tmux 未安装时接口返回 503，静默降级
      $('chipSessions').textContent = '—';
    }
  }

  function renderSessions() {
    const sel = $('selSession');
    const prev = state.current || sel.value;
    sel.innerHTML = '';
    if (!state.sessions.length) {
      const o = document.createElement('option');
      o.textContent = '（无会话）';
      o.value = '';
      sel.appendChild(o);
      return;
    }
    state.sessions.forEach((s) => {
      const o = document.createElement('option');
      o.value = s.name;
      o.textContent = s.name + ' (' + (s.windows || 1) + ' 窗 ' + (s.attached ? '· 已连接' : '') + ')';
      sel.appendChild(o);
    });
    const target = state.sessions.some((s) => s.name === prev) ? prev : state.sessions[0].name;
    sel.value = target;
    state.current = target;
  }

  async function refreshStatus() {
    if (!state.current) return;
    try {
      const d = await api('/api/status?session=' + encodeURIComponent(state.current) + '&lines=40');
      state.lastStatus = d;
      renderPanes(d);
    } catch (e) {
      // 会话可能已被杀掉
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
    }
  }

  function renderPanes(d) {
    const host = $('paneHost');
    const panes = d.panes || [];
    if (!panes.length) {
      host.innerHTML = '<div class="empty"><div class="empty__icon">▦</div>' +
        '<div class="empty__title">无 pane</div></div>';
      return;
    }

    // 首次渲染或数量变化时重建
    if (host.children.length !== panes.length || !host.querySelector('.pane-card')) {
      host.innerHTML = '';
      panes.forEach((p, i) => {
        const card = document.createElement('div');
        card.className = 'pane-card';
        card.setAttribute('data-i', String(i));

        const head = document.createElement('div');
        head.className = 'pane-card__head';
        const idx = document.createElement('span');
        idx.className = 'pane-card__idx';
        idx.textContent = '#' + (p.index != null ? p.index : i);
        const br = document.createElement('span');
        br.className = 'pane-card__branch';
        br.id = 'br-' + i;
        const dot = document.createElement('span');
        dot.className = 'dot dot--pulse';
        dot.id = 'dot-' + i;
        head.appendChild(idx);
        head.appendChild(br);
        head.appendChild(dot);

        const pre = document.createElement('pre');
        pre.className = 'pane-card__out';
        pre.id = 'out-' + i;

        card.appendChild(head);
        card.appendChild(pre);
        host.appendChild(card);
      });
      renderTargets(panes.length);
    }

    panes.forEach((p, i) => {
      const br = $('br-' + i);
      if (br) br.textContent = p.branch || p.role || (p.alive ? 'alive' : 'dead');
      const dot = $('dot-' + i);
      if (dot) {
        dot.style.color = p.alive ? 'var(--c-success)' : 'var(--c-text-faint)';
        dot.className = 'dot' + (p.alive ? ' dot--pulse' : '');
      }
      const out = $('out-' + i);
      if (out && out.textContent !== (p.output || '')) {
        out.textContent = p.output || '';
        out.scrollTop = out.scrollHeight;
      }
      const card = host.querySelector('[data-i="' + i + '"]');
      if (card) card.className = 'pane-card' + (p.alive ? '' : ' is-dead');
    });

    const alive = panes.filter((p) => p.alive).length;
    $('monHint').textContent = panes.length + ' pane · ' + alive + ' 存活 · ' +
      new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }

  function renderTargets(n) {
    const sel = $('selTarget');
    const prev = sel.value;
    sel.innerHTML = '';
    const all = document.createElement('option');
    all.value = '';
    all.textContent = '广播全体';
    sel.appendChild(all);
    for (let i = 0; i < n; i++) {
      const o = document.createElement('option');
      o.value = String(i);
      o.textContent = 'Pane #' + i;
      sel.appendChild(o);
    }
    if (prev) sel.value = prev;
  }

  // ============================================================ 动作
  async function doLaunch() {
    const btn = $('btnLaunch');
    const tasks = collectTasks();
    if (state.mode !== 'parallel' && !tasks.some((t) => t.text.trim())) {
      toast('请先为至少一个 agent 填写 requirement', 'warn');
      return;
    }
    if (state.mode === 'parallel' && !tasks[0].text.trim()) {
      toast('并行模式需要填写第 1 条 requirement（会广播）', 'warn');
      return;
    }

    btn.disabled = true;
    const old = btn.textContent;
    btn.innerHTML = '<span class="spinner"></span>启动中…';
    try {
      const d = await api('/api/launch', {
        method: 'POST',
        body: JSON.stringify({
          mode: state.mode,
          agent: $('selAgent').value,
          count: Number($('rngCount').value),
          repo: $('inpRepo').value.trim() || '.',
          tasks: tasks.map((t) => t.text),
          roles: tasks.map((t) => t.name),
          broadcast: $('swBroadcast').checked,
        }),
      });
      state.current = d.session;
      const n = d.panes ? d.panes.length : (d.count || 0);
      const notReady = (d.ready || []).map((r, i) => (r ? null : '#' + i))
        .filter((x) => x !== null);
      toast('已启动会话 ' + d.session + '（' + n + ' 个 pane）' +
        (notReady.length ? '；pane ' + notReady.join('、') +
          ' 未检测到 agent 进程，未注入任务' : ''),
        notReady.length ? 'warn' : 'ok');
      if (d.service && d.service.required && d.service.detail) {
        toast(d.service.detail, 'ok');
      }
      await loadSessions();
      await refreshStatus();
      startPolling();
    } catch (e) {
      toast('启动失败：' + e.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  async function doSend() {
    const text = $('inpCmd').value.trim();
    if (!text) return;
    if (!state.current) { toast('没有活动会话', 'warn'); return; }
    const target = $('selTarget').value;
    try {
      await api('/api/send', {
        method: 'POST',
        body: JSON.stringify({
          session: state.current,
          text: text,
          pane: target === '' ? null : Number(target),
        }),
      });
      $('inpCmd').value = '';
      toast(target === '' ? '已广播' : '已发往 pane #' + target, 'ok');
      setTimeout(refreshStatus, 600);
    } catch (e) {
      toast('发送失败：' + e.message, 'err');
    }
  }

  async function doStop() {
    if (!state.current) { toast('没有活动会话', 'warn'); return; }
    if (!confirm('停止会话 ' + state.current + '？这会杀掉 tmux 会话。未提交的改动会保留在磁盘上。')) return;
    try {
      const d = await api('/api/stop', {
        method: 'POST',
        body: JSON.stringify({ session: state.current }),
      });
      toast('已停止 ' + state.current, 'ok');
      stopPolling();
      state.current = null;
      $('paneHost').innerHTML = '<div class="empty"><div class="empty__icon">▦</div>' +
        '<div class="empty__title">已停止</div><div class="empty__desc">会话已结束。</div></div>';
      $('monHint').textContent = '已停止';
      await loadSessions();
    } catch (e) {
      toast('停止失败：' + e.message, 'err');
    }
  }

  async function doDiff() {
    if (!state.current) { toast('没有活动会话', 'warn'); return; }
    try {
      const d = await api('/api/diff?session=' + encodeURIComponent(state.current));
      const panes = (d.panes || d.diff || []);
      if (!panes.length) { toast('没有检测到改动', 'warn'); return; }
      const lines = panes.map((p) =>
        '#' + (p.index != null ? p.index : '?') + '  ' + (p.branch || '') +
        '  ' + (p.files != null ? p.files + ' 文件' : '') +
        (p.stat ? '\n    ' + String(p.stat).split('\n').join('\n    ') : ''));
      toast('改动汇总见控制台', 'ok');
      console.log('[diff]\n' + lines.join('\n'));
      alert('改动汇总（详见浏览器控制台）：\n\n' + lines.join('\n'));
    } catch (e) {
      toast('获取 diff 失败：' + e.message, 'err');
    }
  }

  function startPolling() {
    stopPolling();
    if (!$('swAuto').checked) return;
    state.timer = setInterval(refreshStatus, 2000);
  }
  function stopPolling() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  // ============================================================ 主题
  function initTheme() {
    const saved = localStorage.getItem('magent-theme');
    const theme = saved || 'dark';
    document.documentElement.setAttribute('data-theme', theme);
    $('btnTheme').textContent = theme === 'dark' ? '亮色' : '暗色';
    $('btnTheme').onclick = () => {
      const cur = document.documentElement.getAttribute('data-theme');
      const next = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('magent-theme', next);
      $('btnTheme').textContent = next === 'dark' ? '亮色' : '暗色';
    };
  }

  // ============================================================ 设置面板
  // 模型 / API Key / 自定义模型 / 角色库，数据全部来自 /api/config
  const cfg = { agents: [], roles: [], paths: {} };

  async function loadConfig() {
    const d = await api('/api/config');
    cfg.agents = d.agents || [];
    cfg.roles = d.roles || [];
    cfg.paths = d.paths || {};
    if (cfg.roles.length) ROLES = cfg.roles;
    renderSettings();
  }

  function renderSettings() {
    const p = cfg.paths || {};
    $('setCredPath').textContent =
      '凭据 ' + (p.credentials || '~/.magent-console/config.json') +
      (p.config_env ? ' · 由环境变量 ' + p.config_env + ' 指定' : '') +
      (p.credentials_exists ? ' · 已存在' : ' · 尚未创建');
    renderAgentAuth();
    renderCustomList();
    renderRolesEditor();
  }

  function statusChip(text, ok) {
    const s = document.createElement('span');
    s.className = 'chip ' + (ok ? 'chip--ok' : 'chip--err');
    const dot = document.createElement('span');
    dot.className = 'dot';
    s.appendChild(dot);
    s.appendChild(document.createTextNode(text));
    return s;
  }

  function renderAgentAuth() {
    const host = $('agentAuthList');
    host.innerHTML = '';
    cfg.agents.forEach((a) => {
      const row = document.createElement('div');
      row.className = 'stack';
      row.style.cssText = 'gap:6px;padding:8px 0;border-bottom:1px solid var(--c-border)';

      const head = document.createElement('div');
      head.className = 'row row--wrap';
      head.style.cssText = 'gap:6px;align-items:center';

      const title = document.createElement('strong');
      title.style.fontSize = '12px';
      title.textContent = a.name + (a.custom ? '（自定义）' : '');
      head.appendChild(title);
      head.appendChild(statusChip(a.installed ? '已安装' : '未安装', a.installed));
      if (a.auth_env) {
        const src = a.authed
          ? ({ env: '环境变量', file: '本地配置' }[a.auth_source] || a.auth_source || '已设置')
          : '未鉴权';
        head.appendChild(statusChip(src, !!a.authed));
      } else {
        head.appendChild(statusChip(a.local ? '本地模型 · 无需 Key' : '登录型 · 无需 Key', true));
      }

      const cmd = document.createElement('code');
      cmd.className = 'mono t-xs faint';
      cmd.textContent = a.command || a.cmd || '';
      head.appendChild(cmd);

      const btnTest = document.createElement('button');
      btnTest.className = 'btn btn--outlined btn--sm';
      btnTest.type = 'button';
      btnTest.textContent = '测试';
      btnTest.style.marginLeft = 'auto';
      btnTest.onclick = () => testAgent(a.id, null, btnTest);
      head.appendChild(btnTest);
      row.appendChild(head);

      if (a.auth_env) {
        const line = document.createElement('div');
        line.className = 'row';
        line.style.cssText = 'gap:6px;align-items:center';

        const inp = document.createElement('input');
        inp.className = 'input';
        inp.type = 'password';
        inp.autocomplete = 'off';
        inp.style.cssText = 'height:30px;font-size:12px;flex:1 1 auto';
        inp.placeholder = a.auth_value_masked
          ? ('已保存 ' + a.auth_value_masked + '，留空则不改')
          : (a.auth_env + ' 的值，或 env:变量名（不落盘）');
        line.appendChild(inp);

        const btnSave = document.createElement('button');
        btnSave.className = 'btn btn--filled btn--sm';
        btnSave.type = 'button';
        btnSave.textContent = '保存';
        btnSave.onclick = async () => {
          const v = inp.value.trim();
          if (!v) { toast('留空表示不修改，请输入 Key 或 env:变量名', 'err'); return; }
          btnSave.disabled = true;
          try {
            await api('/api/config', {
              method: 'POST',
              body: JSON.stringify({ agent_auth: { [a.id]: v } }),
            });
            inp.value = '';
            await loadConfig();
            await loadAgents();
            toast(a.name + ' 的密钥已保存到 ' + (cfg.paths.credentials || '配置'));
          } catch (e) {
            toast('保存失败：' + e.message, 'err');
          } finally { btnSave.disabled = false; }
        };
        line.appendChild(btnSave);

        const btnClear = document.createElement('button');
        btnClear.className = 'btn btn--text btn--sm';
        btnClear.type = 'button';
        btnClear.textContent = '清除';
        btnClear.onclick = async () => {
          try {
            await api('/api/config', {
              method: 'POST',
              body: JSON.stringify({ agent_auth: { [a.id]: '' } }),
            });
            inp.value = '';
            await loadConfig();
            await loadAgents();
            toast('已清除 ' + a.name + ' 的本地密钥');
          } catch (e) { toast('清除失败：' + e.message, 'err'); }
        };
        line.appendChild(btnClear);
        row.appendChild(line);
      }

      // 本地模型 / 自建服务：可配置服务地址与模型名
      if (a.api_url_env || a.local) {
        const cfgRow = document.createElement('div');
        cfgRow.className = 'row row--wrap';
        cfgRow.style.cssText = 'gap:6px;align-items:center';

        const urlInp = document.createElement('input');
        urlInp.className = 'input';
        urlInp.type = 'text';
        urlInp.style.cssText = 'height:30px;font-size:12px;flex:2 1 200px';
        urlInp.placeholder = '服务地址（' + (a.api_url_env || 'API_URL') +
          '），如 http://127.0.0.1:11434';
        urlInp.value = a.api_url || '';
        cfgRow.appendChild(urlInp);

        const modelInp = document.createElement('input');
        modelInp.className = 'input';
        modelInp.type = 'text';
        modelInp.style.cssText = 'height:30px;font-size:12px;flex:1 1 130px';
        modelInp.placeholder = '模型名，如 qwen3.5:4b';
        modelInp.value = a.model || '';
        cfgRow.appendChild(modelInp);

        const btnSaveCfg = document.createElement('button');
        btnSaveCfg.className = 'btn btn--filled btn--sm';
        btnSaveCfg.type = 'button';
        btnSaveCfg.textContent = '保存配置';
        btnSaveCfg.onclick = async () => {
          const url = urlInp.value.trim();
          const model = modelInp.value.trim();
          if (!url) { toast('请填写服务地址', 'err'); return; }
          btnSaveCfg.disabled = true;
          try {
            await api('/api/config', {
              method: 'POST',
              body: JSON.stringify({
                agent_overrides: { [a.id]: { api_url: url, model: model } },
              }),
            });
            await loadConfig();
            await loadAgents();
            toast(a.name + ' 的服务地址与模型已保存');
          } catch (e) {
            toast('保存失败：' + e.message, 'err');
          } finally { btnSaveCfg.disabled = false; }
        };
        cfgRow.appendChild(btnSaveCfg);

        const btnReset = document.createElement('button');
        btnReset.className = 'btn btn--text btn--sm';
        btnReset.type = 'button';
        btnReset.textContent = '恢复默认';
        btnReset.onclick = async () => {
          try {
            await api('/api/config', {
              method: 'POST',
              body: JSON.stringify({ agent_overrides: { [a.id]: null } }),
            });
            await loadConfig();
            await loadAgents();
            toast(a.name + ' 已恢复内置默认配置');
          } catch (e) { toast('恢复失败：' + e.message, 'err'); }
        };
        cfgRow.appendChild(btnReset);
        row.appendChild(cfgRow);

        if (a.service_start) {
          const tip = document.createElement('div');
          tip.className = 't-xs faint';
          tip.textContent = '未探测到服务时会自动执行：' + a.service_start +
            '（仅对 127.0.0.1 / localhost 生效）';
          row.appendChild(tip);
        }
      }

      if (!a.installed && a.install_hint) {
        const hint = document.createElement('div');
        hint.className = 't-xs faint';
        hint.style.whiteSpace = 'pre-wrap';
        hint.textContent = '安装提示：' + a.install_hint;
        row.appendChild(hint);
      }
      host.appendChild(row);
    });
  }

  async function testAgent(id, spec, btn) {
    if (btn) btn.disabled = true;
    try {
      const d = await api('/api/config/test', {
        method: 'POST',
        body: JSON.stringify(spec ? { agent: id, spec: spec } : { agent: id }),
      });
      toast(d.message || '测试完成', d.ok ? 'ok' : 'err');
    } catch (e) {
      toast('测试失败：' + e.message, 'err');
    } finally { if (btn) btn.disabled = false; }
  }

  function renderCustomList() {
    const host = $('customList');
    host.innerHTML = '';
    const customs = cfg.agents.filter((a) => a.custom);
    if (!customs.length) {
      const empty = document.createElement('div');
      empty.className = 't-xs faint';
      empty.textContent = '暂无自定义模型';
      host.appendChild(empty);
      return;
    }
    customs.forEach((a) => {
      const row = document.createElement('div');
      row.className = 'row row--wrap';
      row.style.cssText = 'gap:6px;align-items:center;padding:6px 0;border-bottom:1px solid var(--c-border)';

      const t = document.createElement('strong');
      t.style.fontSize = '12px';
      t.textContent = a.name;
      row.appendChild(t);

      const c = document.createElement('code');
      c.className = 'mono t-xs faint';
      c.textContent = a.command || a.cmd || '';
      row.appendChild(c);

      const bt = document.createElement('button');
      bt.className = 'btn btn--outlined btn--sm';
      bt.type = 'button';
      bt.textContent = '测试';
      bt.style.marginLeft = 'auto';
      bt.onclick = () => testAgent(a.id, null, bt);
      row.appendChild(bt);

      const bd = document.createElement('button');
      bd.className = 'btn btn--danger btn--sm';
      bd.type = 'button';
      bd.textContent = '删除';
      bd.onclick = async () => {
        const rest = cfg.agents.filter((x) => x.custom && x.id !== a.id)
          .map((x) => ({
            id: x.id, name: x.name, cmd: x.cmd, model: x.model || '',
            args: x.args || [], auth_env: x.auth_env || '', local: !!x.local,
            install_hint: x.install_hint || '', probe_url: x.probe_url || '',
          }));
        try {
          await api('/api/config', {
            method: 'POST',
            body: JSON.stringify({ custom_agents: rest }),
          });
          await loadConfig();
          await loadAgents();
          toast('已删除 ' + a.name);
        } catch (e) { toast('删除失败：' + e.message, 'err'); }
      };
      row.appendChild(bd);
      host.appendChild(row);
    });
  }

  function renderRolesEditor() {
    const host = $('roleList');
    host.innerHTML = '';
    cfg.roles.forEach((r, i) => {
      const box = document.createElement('div');
      box.className = 'stack';
      box.style.cssText = 'gap:4px;padding:6px 0;border-bottom:1px solid var(--c-border)';

      const line = document.createElement('div');
      line.className = 'row';
      line.style.cssText = 'gap:6px;align-items:center';

      const nm = document.createElement('input');
      nm.className = 'input';
      nm.type = 'text';
      nm.value = r.name || '';
      nm.placeholder = '角色名';
      nm.style.cssText = 'height:30px;font-size:12px;flex:1 1 auto';
      nm.oninput = () => { r.name = nm.value; };
      line.appendChild(nm);

      const del = document.createElement('button');
      del.className = 'btn btn--danger btn--sm';
      del.type = 'button';
      del.textContent = '删除';
      del.onclick = () => { cfg.roles.splice(i, 1); renderRolesEditor(); };
      line.appendChild(del);
      box.appendChild(line);

      const ta = document.createElement('textarea');
      ta.className = 'input';
      ta.rows = 3;
      ta.style.cssText = 'font-size:11px;line-height:1.5;resize:vertical';
      ta.placeholder = '该角色的 requirement 模板';
      ta.value = r.requirement || r.text || '';
      ta.oninput = () => { r.requirement = ta.value; };
      box.appendChild(ta);

      host.appendChild(box);
    });
  }

  async function saveCustomAgent(ev) {
    ev.preventDefault();
    const id = $('cfId').value.trim();
    const cmd = $('cfCmd').value.trim();
    if (!id || !cmd) { toast('id 与启动命令必填', 'err'); return; }

    const modelRaw = $('cfModel').value.trim();
    const asArgs = modelRaw.startsWith('-');
    const spec = {
      id: id,
      name: $('cfName').value.trim() || id,
      cmd: cmd,
      model: asArgs ? '' : modelRaw,
      args: asArgs ? modelRaw.split(/\s+/).filter(Boolean) : [],
      auth_env: $('cfAuthEnv').value.trim() || '',
      probe_url: $('cfProbe').value.trim() || '',
      install_hint: $('cfHint').value.trim() || '',
      local: !!$('cfLocal').checked,
    };

    const merged = cfg.agents.filter((a) => a.custom && a.id !== id)
      .map((a) => ({
        id: a.id, name: a.name, cmd: a.cmd, model: a.model || '',
        args: a.args || [], auth_env: a.auth_env || '', local: !!a.local,
        install_hint: a.install_hint || '', probe_url: a.probe_url || '',
      }));
    merged.push(spec);

    try {
      await api('/api/config', {
        method: 'POST',
        body: JSON.stringify({ custom_agents: merged }),
      });
      $('formCustom').reset();
      await loadConfig();
      await loadAgents();
      toast('已保存自定义模型 ' + spec.name + '，可在 agent 下拉框中选择');
    } catch (e) { toast('保存失败：' + e.message, 'err'); }
  }

  async function saveRoles() {
    const roles = cfg.roles.map((r) => ({
      id: r.id, name: (r.name || '').trim(), requirement: r.requirement || '',
    })).filter((r) => r.name);
    if (!roles.length) { toast('角色库不能为空', 'err'); return; }
    try {
      await api('/api/config', { method: 'POST', body: JSON.stringify({ roles: roles }) });
      await loadConfig();
      if (state.mode === 'heterogeneous') renderTasks(Number($('rngCount').value));
      toast('角色库已保存（' + roles.length + ' 个角色）');
    } catch (e) { toast('保存角色库失败：' + e.message, 'err'); }
  }

  // ============================================================ 事件绑定
  function bind() {
    // 模式卡片
    document.querySelectorAll('.mode-card').forEach((card) => {
      card.addEventListener('click', () => {
        document.querySelectorAll('.mode-card').forEach((c) => c.classList.remove('is-active'));
        card.classList.add('is-active');
        state.mode = card.getAttribute('data-mode');
        const n = Number($('rngCount').value);
        // 切换模式时若未填内容，重填角色名
        renderTasks(n);
        $('swBroadcast').checked = state.mode === 'parallel';
      });
    });

    $('rngCount').addEventListener('input', () => {
      updateCountHint();
    });
    $('rngCount').addEventListener('change', () => {
      renderTasks(Number($('rngCount').value));
    });

    $('btnTpl').addEventListener('click', () => {
      const n = Number($('rngCount').value);
      const items = document.querySelectorAll('#taskList .task-item');
      items.forEach((el, i) => {
        const nm = el.querySelector('.task-item__name-input');
        const ta = el.querySelector('textarea');
        if (state.mode === 'parallel') {
          if (i === 0) {
            if (!ta.value.trim()) ta.value = PARALLEL_TPL;
          } else {
            ta.value = '';
            ta.placeholder = '并行模式：第 1 条会广播，此处留空';
          }
          if (nm) nm.value = 'Agent ' + (i + 1);
        } else {
          const tpl = ROLES[i % ROLES.length] || { name: 'Agent ' + (i + 1), text: '' };
          if (nm) nm.value = tpl.name;
          if (!ta.value.trim()) ta.value = tpl.text;
        }
      });
      toast('已填充模板，请替换为你的实际需求', 'ok');
    });

    $('btnClear').addEventListener('click', () => {
      document.querySelectorAll('#taskList textarea').forEach((ta) => { ta.value = ''; });
    });

    $('btnLaunch').addEventListener('click', doLaunch);
    $('btnSend').addEventListener('click', doSend);
    $('btnStop').addEventListener('click', doStop);
    $('btnDiff').addEventListener('click', doDiff);
    $('btnRefresh').addEventListener('click', async () => {
      await loadProfile();
      await loadAgents();
      await loadSessions();
      toast('已重新检测', 'ok');
    });

    $('selSession').addEventListener('change', () => {
      state.current = $('selSession').value || null;
      $('paneHost').innerHTML = '';
      refreshStatus();
      startPolling();
    });

    $('inpCmd').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); }
    });

    $('swAuto').addEventListener('change', () => {
      if ($('swAuto').checked) startPolling(); else stopPolling();
    });

    // ---- 设置抽屉 ----
    $('btnSettings').addEventListener('click', async () => {
      openDrawer(true);
      try { await loadConfig(); }
      catch (e) { toast('配置加载失败：' + e.message, 'err'); }
    });
    $('btnDrawerClose').addEventListener('click', () => openDrawer(false));
    $('drawerMask').addEventListener('click', () => openDrawer(false));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') openDrawer(false);
    });
    document.querySelectorAll('#drawerSettings .tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('#drawerSettings .tab').forEach((t) => t.classList.remove('is-active'));
        document.querySelectorAll('#drawerBody .tab-pane').forEach((p) => p.classList.remove('is-active'));
        tab.classList.add('is-active');
        const pane = document.querySelector('#drawerBody .tab-pane[data-pane="' + tab.getAttribute('data-tab') + '"]');
        if (pane) pane.classList.add('is-active');
      });
    });
    $('formCustom').addEventListener('submit', saveCustomAgent);
    $('btnRoleAdd').addEventListener('click', () => {
      cfg.roles.push({ id: 'role-' + Date.now(), name: '', requirement: '' });
      renderRolesEditor();
    });
    $('btnRoleSave').addEventListener('click', saveRoles);

    window.addEventListener('beforeunload', stopPolling);
  }

  function openDrawer(open) {
    const drawer = $('drawerSettings');
    const mask = $('drawerMask');
    if (open) {
      drawer.hidden = false;
      mask.hidden = false;
      void drawer.offsetWidth;           // 强制 reflow，保证过渡动画生效
      drawer.classList.add('is-open');   // CSS 里 .is-open 才 translateX(0)
      mask.classList.add('is-open');
    } else {
      drawer.classList.remove('is-open');
      mask.classList.remove('is-open');
      setTimeout(() => {                // 等过渡结束再隐藏，避免动画被截断
        drawer.hidden = true;
        mask.hidden = true;
      }, 220);
    }
  }

  // ============================================================ 启动
  async function main() {
    initTheme();
    bind();

    // 健康检查
    try {
      const h = await api('/api/health');
      const chip = $('chipTmux');
      if (h.tmux) {
        chip.className = 'chip chip--ok';
        chip.innerHTML = '';
        const d = document.createElement('span');
        d.className = 'dot dot--pulse';
        chip.appendChild(d);
        chip.appendChild(document.createTextNode('tmux ' + (h.tmux_version || '')));
      } else {
        chip.className = 'chip chip--err';
        chip.textContent = 'tmux 未安装';
        toast('未检测到 tmux，编排功能不可用。请运行 ./install.sh', 'err');
      }
    } catch (e) {
      toast('后端连接失败：' + e.message, 'err');
      return;
    }

    await loadAgents();
    await loadRoles();
    await loadProfile();
    await loadSessions();
    if (state.current) startPolling();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', main);
  } else {
    main();
  }
})();
