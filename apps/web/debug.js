'use strict';
window.FleetDebug = (() => {
  const M = window.FleetDebugModel;
  const live = new Set(['opening', 'active', 'closing', 'expired']);
  const names = {opening:'正在连接', active:'已连接', closing:'正在断开', expired:'等待释放', closed:'已结束', lost:'设备已重启'};
  const txNames = {queued:'等待下发', dispatched:'等待确认', sent:'发送完成', failed:'发送失败', expired:'已过期', unknown:'结果未知'};
  const date = t => new Date(t * 1000).toLocaleString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai'});
  const clock = t => new Date(t * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false, timeZone:'Asia/Shanghai', fractionalSecondDigits:3});
  let data = {bench:{}, sessions:[]}, devices = [], online = false, hooks, focused = '';
  const cards = new Map(), caches = new Map(), benchNodes = new Map();

  function configure(value) {
    hooks = value;
    try { focused = localStorage.getItem('fleet-debug-device') || ''; } catch {}
    document.querySelector('#debug-device-search').oninput = renderBench;
  }
  function focus(id, openUART = false) {
    if (!devices.some(d => d.id === id)) return;
    focused = id;
    try { localStorage.setItem('fleet-debug-device', id); } catch {}
    if (openUART) document.querySelector('#show-uart').click();
    render();
  }
  const latest = id => data.sessions.find(s => s.device_id === id && live.has(s.state)) || data.sessions.find(s => s.device_id === id);
  const busy = id => data.sessions.some(s => s.device_id === id && live.has(s.state));
  function renderBench() {
    if (devices.length && !devices.some(d=>d.id===focused)) focused = devices[0].id;
    const query=document.querySelector('#debug-device-search').value.trim().toLocaleLowerCase();
    for (const [id,node] of benchNodes)if(!devices.some(d=>d.id===id)){node.remove();benchNodes.delete(id);}
    for (const d of devices) {
      const id=d.id;
      const s = latest(id);
      let node = benchNodes.get(id);
      if (!node) {
        node = document.createElement('button'); node.className = 'bench-device';
        node.innerHTML = '<span class="bench-letter"></span><span class="bench-identity"><strong></strong><small></small><code></code></span><span class="bench-state"><span class="pill"></span><small></small></span>';
        node.onclick = () => focus(id);
        document.querySelector('#bench-devices').append(node); benchNodes.set(id, node);
      }
      node.setAttribute('aria-label', `选择 ${d?.name || id}`);
      node.setAttribute('aria-pressed', String(focused === id));
      node.hidden=!!query&&!`${d.name} ${id} ${d.group_name}`.toLocaleLowerCase().includes(query);
      node.querySelector('.bench-letter').textContent = 'S3';
      node.querySelector('strong').textContent = d?.name || id;
      node.querySelector('.bench-identity small').textContent = d.group_name || 'ESP32-S3';
      node.querySelector('code').textContent = id;
      const badge = node.querySelector('.pill');
      badge.textContent = !online ? '状态未知' : d?.revoked ? '已撤销' : d?.online ? '在线' : '离线';
      badge.className = 'pill ' + (online && d?.online && !d.revoked ? 'online' : 'unknown');
      node.querySelector('.bench-state small').textContent = s && live.has(s.state) ? names[s.state] + (s.mine ? '' : ' · 其他浏览器') : '未连接调试';
    }
    document.querySelector('#debug-device-empty').hidden=[...benchNodes.values()].some(node=>!node.hidden);
    window.FleetConnections.select(focused);
  }
  function viewed(card) {
    const id = card.selectedId || latest(card.id)?.id;
    return data.sessions.find(s => s.id === id) || card.history.find(s => s.id === id) || caches.get(id)?.session;
  }
  function savePresets(card) {
    try { localStorage.setItem('fleet-uart-presets:' + card.id, JSON.stringify(card.presets)); }
    catch { card.q('[data-error]').textContent = '浏览器未能保存快捷输入，本次仍可使用。'; }
    renderPresets(card);
  }
  function renderPresets(card) {
    const list = card.q('[data-commands]'); list.replaceChildren();
    if (!card.presets.length) {
      const empty = document.createElement('p'); empty.className = 'command-empty';
      empty.textContent = '在发送编辑器输入内容后，点击“保存当前输入”。'; list.append(empty);
    }
    card.presets.forEach((item, index) => {
      const wrap = document.createElement('div'), use = document.createElement('button'), code = document.createElement('code'), meta = document.createElement('small'), remove = document.createElement('button');
      wrap.className = 'command-item'; code.textContent = item.text;
      meta.textContent = `${item.mode === 'hex' ? 'HEX' : 'UTF-8'}${item.mode === 'text' && item.newline ? ' · CRLF' : ''}`;
      use.append(code, meta); use.setAttribute('aria-label', `填入快捷输入 ${index + 1}`);
      use.onclick = () => {
        if (card.pending || card.retry) return;
        card.q('[data-payload]').value = item.text; card.q('[data-format]').value = item.mode;
        card.q('[data-newline]').checked = item.newline; composer(card); card.q('[data-payload]').focus();
      };
      remove.textContent = '×'; remove.setAttribute('aria-label', `移除快捷输入 ${index + 1}`);
      remove.onclick = () => { card.presets.splice(index, 1); savePresets(card); };
      wrap.append(use, remove); list.append(wrap);
    });
  }
  function cardFor(id, role) {
    if (cards.has(id)) return cards.get(id);
    const node = document.createElement('article'); node.className = 'uart-card';
    node.innerHTML = `
      <header class="uart-header"><div class="uart-identity"><h2></h2><span class="pill" data-state></span><span class="uart-spec" data-spec></span></div><div class="uart-actions"><button data-open>连接</button><button class="quiet" data-close>断开</button></div></header>
      <div class="uart-session" data-session></div>
      <div class="uart-history"><label><span>会话记录</span><select data-history aria-label="串口会话记录"></select></label><button class="quiet" data-older>更早记录</button><button class="quiet" data-current hidden>返回最近会话</button><button class="quiet" data-export>导出保留记录</button></div>
      <div class="uart-body"><div class="uart-stream">
        <div class="uart-tools"><select data-display aria-label="串口显示格式"><option value="timeline">收发时间线</option><option value="hex">HEX 时间线</option><option value="text">按通道阅读 RX</option><option value="raw">原始 RX 块</option></select><select data-port aria-label="接收通道"><option value="all">全部通道</option><option value="1">UART1 · 数据</option></select><input data-search type="search" aria-label="搜索接收内容" placeholder="筛选记录内容…"><button class="quiet" data-pause>暂停显示</button><button class="quiet" data-clear>清屏</button><button class="quiet" data-toggle-presets aria-expanded="false">快捷输入</button></div>
        <div class="uart-record"><span data-record></span><details class="record-scope"><summary data-scope-title>记录范围</summary><p data-scope></p></details></div>
        <div class="uart-output" tabindex="0" role="log" aria-live="off" aria-label="串口收发记录"></div>
        <div class="uart-view-options"><span data-rx-count></span><label><input data-wrap type="checkbox" checked>自动换行</label><label><input data-follow type="checkbox" checked>跟随最新</label></div>
        <form class="uart-send"><div class="send-input"><textarea data-payload aria-label="待发送数据" placeholder="输入待发送内容 · Ctrl + Enter 发送" maxlength="768" spellcheck="false"></textarea><button data-send>发送</button></div><div class="send-format"><select data-format aria-label="串口发送格式"><option value="text">UTF-8 文本</option><option value="hex">HEX 字节</option></select><label><input type="checkbox" data-newline checked>追加 CRLF</label><span data-byte-count></span><span data-send-reason></span></div></form>
        <p class="error-text" data-error role="status"></p>
      </div><aside class="command-pane"><h3>快捷输入</h3><p>填入编辑器后确认发送。</p><button class="quiet" data-save>保存当前输入</button><div data-commands class="command-list"></div><details class="context-help uart-wiring"><summary>本设备引脚</summary><p data-wiring></p></details></aside></div>`;
    const card = {node, id, role, pending:false, retry:null, session:'', selectedId:'', key:'', history:[], historyReady:false, historyLoading:false, historyNext:null, paused:null, cleared:0, clearedTX:new Set(), presets:[]};
    card.q = selector => node.querySelector(selector); const q = card.q;
    if (role === 'probe') q('[data-port]').add(new Option('启动串口', '2'));
    const uart=window.FleetConnections.profile(id)?.uart;
    q('[data-wiring]').textContent = uart?`TX GPIO${uart.tx} · RX GPIO${uart.rx}${role===uart.monitor_role?' · 附加 RX GPIO'+uart.monitor_rx:''}`:'未配置 UART 接口';
    q('[data-open]').onclick = () => action(card, async () => { await hooks.api('/api/debug/sessions', {device_id:id}); card.selectedId = ''; card.historyReady = false; });
    q('[data-close]').onclick = () => action(card, async () => { const s = latest(id); if (s) await hooks.api(`/api/debug/sessions/${s.id}/close`, {}); });
    q('[data-history]').onchange = () => { card.selectedId = q('[data-history]').value; render(); };
    q('[data-current]').onclick = () => { card.selectedId = ''; render(); };
    q('[data-older]').onclick = () => history(card, card.historyReady);
    for (const field of ['display', 'port', 'search']) q(`[data-${field}]`).oninput = () => renderOutput(card);
    q('[data-wrap]').onchange = () => node.classList.toggle('no-wrap', !q('[data-wrap]').checked);
    q('[data-follow]').onchange = () => { if (q('[data-follow]').checked) follow(card); };
    q('.uart-output').onscroll = () => { const e = q('.uart-output'); if (!card.autoScrolling && e.scrollHeight - e.clientHeight - e.scrollTop > 35) q('[data-follow]').checked = false; };
    q('[data-pause]').onclick = () => {
      const cache = caches.get(card.session);
      card.paused = card.paused ? null : {rows:[...(cache?.rows || [])], transmissions:(cache?.transmissions || []).map(t => ({...t}))};
      q('[data-pause]').textContent = card.paused ? '恢复显示' : '暂停显示'; renderOutput(card);
    };
    q('[data-clear]').onclick = () => {
      const cache = card.paused || caches.get(card.session);
      card.cleared = cache?.rows.at(-1)?.id || 0; card.clearedTX = new Set((cache?.transmissions || []).map(t => t.id)); card.key = ''; renderOutput(card);
    };
    try {
      const saved = JSON.parse(localStorage.getItem('fleet-uart-presets:' + id) || '[]');
      if (Array.isArray(saved)) card.presets = saved.filter(v => typeof v.text === 'string' && v.text.length <= 768 && ['text', 'hex'].includes(v.mode) && typeof v.newline === 'boolean').slice(0, 20);
    } catch {}
    for (const field of ['format', 'newline', 'payload']) q(`[data-${field}]`).oninput = () => composer(card);
    q('[data-payload]').onkeydown = e => { if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); if (!q('[data-send]').disabled) send(card); } };
    q('form').onsubmit = e => { e.preventDefault(); send(card); };
    q('[data-toggle-presets]').onclick = () => { node.classList.toggle('presets-open'); q('[data-toggle-presets]').setAttribute('aria-expanded', String(node.classList.contains('presets-open'))); };
    q('[data-save]').onclick = () => {
      const item = {text:q('[data-payload]').value, mode:q('[data-format]').value, newline:q('[data-newline]').checked};
      try { M.payload(item.text, item.mode, item.newline); } catch (e) { q('[data-error]').textContent = e.message; return; }
      if (card.presets.some(p => JSON.stringify(p) === JSON.stringify(item))) return hooks.notify('这条输入已保存。');
      if (card.presets.length >= 20) return hooks.notify('最多保存 20 条，请先移除不用的输入。');
      card.presets.push(item); savePresets(card);
    };
    q('[data-export]').onclick = () => exportRecord(card);
    renderPresets(card); cards.set(id, card); document.querySelector('#debug-cards').append(node); return card;
  }
  async function history(card, older = false) {
    if (card.historyLoading || !online || older && !card.historyNext) return;
    card.historyLoading = true;
    try {
      const answer = await hooks.api(`/api/debug/sessions?device_id=${encodeURIComponent(card.id)}${older ? '&before=' + encodeURIComponent(card.historyNext) : ''}`);
      const merged = new Map((older ? card.history : []).map(s => [s.id, s]));
      answer.sessions.forEach(s => merged.set(s.id, s)); card.history = [...merged.values()];
      card.historyReady = true; card.historyNext = answer.next;
    } catch (e) { card.q('[data-error]').textContent = '历史记录读取失败：' + e.message; }
    finally { card.historyLoading = false; renderHistory(card); }
  }
  function renderHistory(card) {
    const rows = new Map(card.history.map(s => [s.id, s]));
    data.sessions.filter(s => s.device_id === card.id).forEach(s => rows.set(s.id, s));
    const options = [...rows.values()].sort((a, b) => b.created - a.created), select = card.q('[data-history]');
    const signature = JSON.stringify(options.map(s => [s.id, s.state]));
    if (select.signature !== signature) {
      select.signature = signature; select.replaceChildren(new Option('最近会话 · 自动跟随', ''), ...options.map(s => new Option(`${date(s.created)} · ${names[s.state] || s.state} · ${s.id.slice(0, 6)}`, s.id)));
    }
    select.value = card.selectedId;
    card.q('[data-older]').hidden = card.historyReady && !card.historyNext;
    card.q('[data-older]').textContent = card.historyReady ? '更早记录' : '加载历史';
    card.q('[data-older]').disabled = card.historyLoading || !online;
    card.q('[data-current]').hidden = !card.selectedId;
  }
  async function action(card, fn) {
    if (card.pending || !online) return;
    card.pending = true; card.q('[data-error]').textContent = ''; render();
    try { await fn(); } catch (e) { card.q('[data-error]').textContent = e.message; }
    finally { card.pending = false; await hooks.refresh(); }
  }
  function composer(card) {
    const q = card.q, s = latest(card.id), d = devices.find(d => d.id === card.id), input = q('[data-payload]').value;
    let length = 0, valid = false;
    try { length = M.payload(input, q('[data-format]').value, q('[data-newline]').checked).length; valid = true; } catch {}
    let reason = '';
    if (!window.FleetConnections.available(card.id,'uart')) reason = '此设备未配置 UART 接口';
    else if (card.session && card.session !== s?.id) reason = '历史会话只读';
    else if (!online || !d?.online || d.revoked) reason = '等待设备在线';
    else if (s?.state !== 'active') reason = '连接调试后可发送';
    else if (!s.mine) reason = '其他浏览器控制中';
    else if (s.tx.some(t => ['queued', 'dispatched'].includes(t.state))) reason = '等待上一条发送确认';
    card.canSend = !reason && !card.pending;
    q('[data-send-reason]').textContent = reason;
    q('[data-byte-count]').textContent = valid ? `${length} / 256 B` : input ? '格式无效或超过 256 B' : '0 / 256 B';
    q('[data-byte-count]').className = input && !valid ? 'invalid' : '';
    q('[data-send]').disabled = !card.canSend || !valid && !card.retry;
    q('[data-send]').textContent = card.pending ? '提交中…' : card.retry ? '核查发送' : '发送';
    q('[data-save]').disabled = !valid;
    q('[data-payload]').disabled = q('[data-format]').disabled = card.pending || Boolean(card.retry);
    q('[data-newline]').disabled = card.pending || Boolean(card.retry) || q('[data-format]').value === 'hex';
  }
  async function send(card) {
    composer(card); if (!card.canSend) return;
    const s = latest(card.id); let bytes;
    try { bytes = card.retry?.bytes || M.payload(card.q('[data-payload]').value, card.q('[data-format]').value, card.q('[data-newline]').checked); }
    catch (e) { card.q('[data-error]').textContent = e.message; return; }
    const request = card.retry || {session:s.id, bytes, request_id:crypto.randomUUID(), data:Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')};
    card.retry = request;
    await action(card, async () => {
      await hooks.api(`/api/debug/sessions/${request.session}/send`, {data:request.data, request_id:request.request_id});
      card.retry = null; card.q('[data-payload]').value = '';
    });
  }
  async function load(card, s) {
    let cache = caches.get(s.id);
    if (!cache) { cache = {rows:[], cursor:0, transmissions:[], scope:null, loading:false}; caches.set(s.id, cache); }
    if (cache.loading) return;
    if (caches.size > 12) for (const [id, old] of caches) if (!old.loading && ![...cards.values()].some(c => c.session === id)) { caches.delete(id); break; }
    cache.loading = true;
    try {
      for (let page = 0; page < 8; page++) {
        const answer = await hooks.api(`/api/debug/sessions/${s.id}/record?after=${cache.cursor}`);
        for (const row of answer.frames) if (row.id > cache.cursor) { cache.rows.push(row); cache.cursor = row.id; }
        cache.rows = cache.rows.filter(r => answer.scope.first_id && r.id >= answer.scope.first_id).slice(-2000);
        cache.scope = answer.scope; cache.transmissions = answer.transmissions; cache.session = answer.session;
        if (!answer.has_more) break;
      }
      if (card.session === s.id) renderOutput(card);
    } catch (e) { card.q('[data-error]').textContent = '记录读取失败：' + e.message; }
    finally { cache.loading = false; }
  }
  async function exportRecord(card) {
    const sid = card.session; if (!sid || card.exporting) return;
    card.exporting = true; card.q('[data-export]').disabled = true;
    try {
      const record = await hooks.api(`/api/debug/sessions/${sid}/record?limit=2000`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], {type:'application/json'}));
      const a = document.createElement('a'); a.href = url; a.download = `uart-retained-${card.id}-${sid}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      hooks.notify(`已导出保留记录：${record.scope.retained_frames} 个接收块、${record.scope.tx_retained} 条发送。${M.coverage(record.scope).partial ? '存在缺口，详见文件 scope。' : ''}`);
    } catch (e) { card.q('[data-error]').textContent = '导出失败：' + e.message; }
    finally { card.exporting = false; renderOutput(card, true); }
  }
  function follow(card) {
    if (card.node.hidden) return;
    card.autoScrolling = true; const output = card.q('.uart-output'); output.scrollTop = output.scrollHeight;
    requestAnimationFrame(() => card.autoScrolling = false);
  }
  function renderOutput(card, force = false) {
    const q = card.q, cache = caches.get(card.session), scope = cache?.scope, view = card.paused || cache;
    const rows = (view?.rows || []).filter(r => r.id > card.cleared), tx = (view?.transmissions || []).filter(t => !card.clearedTX.has(t.id));
    const mode = q('[data-display]').value, port = q('[data-port]').value, search = q('[data-search]').value.toLocaleLowerCase();
    q('[data-export]').disabled = !card.session || !scope || card.exporting || !online;
    const coverage = M.coverage(scope);
    q('[data-scope-title]').textContent = scope ? `RX ${scope.retained_frames} / ${scope.retention_limit} 块 · TX ${scope.tx_retained} 条${coverage.partial ? ' · 有缺口' : ''}` : '记录范围';
    q('.record-scope').classList.toggle('partial', coverage.partial);
    q('[data-scope]').textContent = scope ? `服务器保留 RX ${scope.retained_frames} / ${scope.retention_limit} 块，共 ${scope.retained_bytes} 字节。${scope.first_seq ? `序号 ${scope.first_seq}–${scope.last_seq}；上报时间 ${date(scope.first_received)} 至 ${date(scope.last_received)}。` : ''}前段不可用 ${scope.unavailable_before} 块，区间缺口 ${scope.gaps_within} 块，设备报告丢弃 ${scope.device_reported_dropped} 块。TX 保留 ${scope.tx_retained} 条，另有 ${scope.tx_omitted} 条未包含。缺口可能来自保留上限或未收到的上报；导出不受搜索、暂停、清屏影响。未上报的数据无法由此判断。` : '正在读取服务器范围。';
    const key = JSON.stringify([card.session, !!scope, rows.map(r => r.id), tx.map(t => [t.id,t.state]), mode, port, search, !!card.paused, card.cleared]);
    if (!force && card.key === key) return; card.key = key;
    const output = q('.uart-output'); output.replaceChildren();
    let count = 0;
    if (mode === 'timeline' || mode === 'hex') {
      const events = M.timeline(rows, tx, mode === 'hex', port).filter(e => !search || (e.content + ' ' + (txNames[e.state] || '')).toLocaleLowerCase().includes(search)); count = events.length;
      const fragment = document.createDocumentFragment();
      for (const e of events) {
        const item = document.createElement('article'), meta = document.createElement('div'), text = document.createElement('pre'); item.className = 'uart-event ' + e.kind;
        meta.className = 'event-meta';
        meta.textContent = `${clock(e.created)}  ${e.kind === 'tx' ? 'TX 发送 · ' + (txNames[e.state] || e.state) : 'RX ' + (e.port === 2 ? '启动串口' : 'UART1') + ` · #${e.seq} · 设备 ${(e.us/1e6).toFixed(3)} s`}  · ${e.bytes}\u00a0B`;
        text.textContent = e.content || '（UTF-8 或控制序列续块）';
        item.append(meta, text); fragment.append(item);
      }
      output.append(fragment);
    } else {
      const text = M.format(rows, mode, port), lines = search ? text.split('\n').filter(l => l.toLocaleLowerCase().includes(search)) : null;
      const pre = document.createElement('pre'); pre.textContent = lines ? lines.join('\n') : text; count = pre.textContent ? (lines ? lines.length : rows.filter(r => port === 'all' || r.port === Number(port)).length) : 0; output.append(pre);
    }
    if (!count) { const p = document.createElement('p'); p.className = 'terminal-empty'; p.textContent = search ? '没有匹配的记录。清除筛选可返回全部内容。' : card.cleared || card.clearedTX.size ? '视图已清空，服务器保留记录仍可导出。' : scope ? '此会话尚无收发记录。' : card.session ? '正在加载会话记录…' : '连接调试后开始记录。'; output.replaceChildren(p); }
    q('[data-rx-count]').textContent = `${search ? '匹配 ' : '显示 '}${count} ${mode === 'text' && search ? '行' : '条'}${card.paused ? ' · 显示已冻结，接收继续' : ''}`;
    if (q('[data-follow]').checked && !card.paused) follow(card);
  }
  function render() {
    if (!hooks) return; renderBench();
    for (const device of devices) {
      const id=device.id,role=data.bench[id]||'';
      const card = cardFor(id, role), q = card.q, d = devices.find(d => d.id === id), control = latest(id), held = control && live.has(control.state), s = viewed(card);
      card.node.hidden = focused !== id;
      q('h2').textContent = `${d?.name || id} · UART`;
      const uart=window.FleetConnections.profile(id)?.uart;q('[data-spec]').textContent=uart?`${uart.baud} · ${uart.format}`:'未配置接口';
      q('[data-state]').textContent = !online ? '状态未知' : held ? names[control.state] : '未连接';
      q('[data-state]').className = 'pill ' + (online && control?.state === 'active' ? 'online' : 'neutral');
      q('[data-session]').textContent = held ? `剩余 ${Math.max(0,Math.ceil(control.expires-data.server_time))} 秒 · ${control.mine ? '本浏览器控制' : '其他浏览器控制'}` : '连接后可收发；历史记录可直接查看。';
      q('[data-session]').hidden = !held;
      q('[data-open]').disabled = card.pending || !online || !d?.online || !window.FleetConnections.available(id,'uart') || held;
      q('[data-close]').hidden = !held; q('[data-close]').disabled = card.pending || !online || !control?.mine || ['closing','expired'].includes(control?.state);
      if (card.session !== (s?.id || '')) {
        card.session = s?.id || ''; card.retry = null; card.cleared = 0; card.clearedTX.clear(); card.paused = null; card.key = ''; q('[data-pause]').textContent = '暂停显示'; q('[data-error]').textContent = '';
      }
      if (card.retry && control?.tx.some(t => t.request_id === card.retry.request_id)) card.retry = null;
      q('[data-record]').textContent = s ? `${date(s.created)} · ${names[s.state]} · ${s.id.slice(0, 8)} · 服务器时间` : '尚无会话记录';
      renderHistory(card); composer(card); renderOutput(card);
      const visible = location.hash === '#debug' && !document.querySelector('#uart-view').hidden && focused === id;
      if (visible && online) { if (!card.historyReady) history(card); if (s) load(card, s); }
    }
  }
  function receive(value, ds, ok) { if (value) data = value; devices = ds; online = ok; render(); }
  return {configure, receive, busy, render, focus};
})();
