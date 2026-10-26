(function (root, factory) {
  const model = factory();
  if (typeof module === 'object' && module.exports) module.exports = model;
  else root.FleetUI = model;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const activeStates = new Set(['queued', 'dispatched', 'running', 'rebooting']);
  function isTestFirmware(release) {
    return /(?:^|[-_.])(?:test|dev|alpha|beta|rc)(?:$|[-_.]|\d)/i.test(release.version);
  }
  function compareVersions(a, b) {
    const parse = v => /^\d+\.\d+\.\d+$/.test(v) ? v.split('.').map(Number) : null;
    const av = parse(a), bv = parse(b);
    if (!av || !bv) return null;
    for (let i = 0; i < 3; i++) if (av[i] !== bv[i]) return av[i] > bv[i] ? 1 : -1;
    return 0;
  }
  function publishable(releases) {
    return releases.filter(f => !isTestFirmware(f) && !f.withdrawn).slice().sort((a, b) => {
      const comparison = compareVersions(b.version, a.version);
      return comparison === null || comparison === 0 ? b.created - a.created : comparison;
    });
  }
  function status(device, connected = true) {
    if (device.revoked) return 'revoked';
    if (!device.last_seen) return 'unseen';
    if (!connected) return 'unknown';
    return device.online ? 'online' : 'offline';
  }
  function filtered(devices, filters, connected = true) {
    const query = (filters.query || '').trim().toLowerCase();
    return devices.filter(d =>
      (!query || `${d.name} ${d.id} ${d.group_name}`.toLowerCase().includes(query)) &&
      (!filters.group || d.group_name === filters.group) &&
      (!filters.status || filters.status === 'all' || status(d, connected) === filters.status)
    ).slice().sort((a, b) => filters.sort === 'heartbeat' ? b.last_seen - a.last_seen || a.name.localeCompare(b.name, 'zh-CN') : a.name.localeCompare(b.name, 'zh-CN'));
  }
  function restrictSelection(selected, page) {
    const allowed = new Set(page.filter(d => !d.revoked).map(d => d.id));
    return new Set([...selected].filter(id => allowed.has(id)));
  }
  function emptyMessage(total) {
    return total ? '没有匹配的设备，请调整筛选条件或清除搜索。' : '尚未登记设备。点击“登记设备”开始首次接入。';
  }
  function plan(kind, ids, devices, releases, jobs, firmwareId) {
    if (!['ota', 'diagnostics', 'restart'].includes(kind)) throw Error('不支持的操作。');
    const unique = [...new Set(ids)];
    if (!unique.length) throw Error('请先选择当前页设备。');
    if (unique.length > 100) throw Error('每批最多选择 100 台设备。');
    const release = kind === 'ota' ? releases.find(f => f.id === firmwareId) : null;
    if (kind === 'ota' && (!release || isTestFirmware(release) || release.withdrawn)) throw Error('请选择可发布的目标固件。');
    const targets = unique.map(id => {
      const d = devices.find(item => item.id === id);
      if (!d || d.revoked) throw Error('部分设备已移除或凭证已撤销，请重新选择。');
      if (jobs.some(j => j.device_id === id && activeStates.has(j.state))) throw Error(`${d.name} 还有未完成的任务，请等待结果。`);
      return { id: d.id, name: d.name, version: d.version || '未知', online: Boolean(d.online), target: release ? release.version : d.version || '未知' };
    });
    const warnings = [];
    const offline = targets.filter(d => !d.online);
    if (offline.length) warnings.push(`${offline.length} 台设备当前未在线，任务会排队等待其接入。`);
    if (kind === 'ota') {
      const down = targets.filter(d => compareVersions(d.target, d.version) === -1);
      if (down.length) warnings.push(`${down.length} 台设备将降级，请核对版本变化。`);
      const same = targets.filter(d => d.target === d.version);
      if (same.length) warnings.push(`${same.length} 台设备已是目标版本，本次仍会重新安装并重启。`);
    }
    if (kind === 'restart') warnings.push('设备重启期间，业务和网络连接会短暂中断。');
    const result = { kind, ids: unique, firmwareId: release ? release.id : null, targets, warnings };
    result.fingerprint = JSON.stringify({ kind, firmwareId: result.firmwareId, targets });
    return result;
  }
  function logLevel(line) {
    const text = line.replace(/\x1b\[[0-9;]*m/g, '');
    if (/^\s*E\s*\(/.test(text)) return 'error';
    if (/^\s*W\s*\(/.test(text)) return 'warn';
    return 'info';
  }
  return { activeStates, isTestFirmware, compareVersions, publishable, status, filtered, restrictSelection, emptyMessage, plan, logLevel };
});
