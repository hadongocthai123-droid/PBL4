const $ = (selector) => document.querySelector(selector);

const LOCK_SVG_CLOSED = `
  <svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <rect width="18" height="11" x="3" y="11" rx="2" ry="2"></rect>
    <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
  </svg>`;

const LOCK_SVG_OPEN = `
  <svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <rect width="18" height="11" x="3" y="11" rx="2" ry="2"></rect>
    <path d="M7 11V7a5 5 0 0 1 9.9-1"></path>
  </svg>`;

let socket = null;
let toastTimer = null;
let thresholdTimer = null;
let voiceEnrollmentSamples = [];
let latestBoxes = [];
let unlockDeadlineMs = 0;
let unlockTotalMs = 5000;
let isLocked = true;

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

async function api(url, opt = {}) {
  const isForm = opt.body instanceof FormData;
  const response = await fetch(url, {
    ...opt,
    headers: {
      ...(isForm ? {} : { 'Content-Type': 'application/json' }),
      ...(opt.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || 'Yêu cầu thất bại');
  }
  return data;
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2800);
}

function hidePendingVoice() {
  const el = $('#pendingVoice');
  el.textContent = '';
  el.classList.add('hidden');
}

function renderOverlay() {
  const img = $('#video');
  const canvas = $('#overlay');
  const rect = img.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));

  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);

  const iw = img.naturalWidth;
  const ih = img.naturalHeight;
  if (!iw || !ih || !latestBoxes.length) return;

  const scale = Math.min(rect.width / iw, rect.height / ih);
  const ox = (rect.width - iw * scale) / 2;
  const oy = (rect.height - ih * scale) / 2;

  ctx.font = '12px ui-monospace, Consolas, monospace';
  for (const box of latestBoxes) {
    const [x1, y1, x2, y2] = box.box.map((v) => v * scale);
    const color =
      box.kind === 'known' ? '#43e1d1' : box.kind === 'spoof' ? '#ff6476' : '#f4c85a';

    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(ox + x1, oy + y1, x2 - x1, y2 - y1);

    const scoreText = (box.face_score || 0).toFixed(2);
    const spoofText = box.spoof_score == null ? '' : ` · Real ${box.spoof_score.toFixed(2)}`;
    const label = `${box.label || 'Unknown'} · ${scoreText}${spoofText}`;
    const textWidth = ctx.measureText(label).width;
    const labelY = Math.max(0, oy + y1 - 21);

    ctx.fillStyle = color;
    ctx.fillRect(ox + x1, labelY, textWidth + 12, 20);
    ctx.fillStyle = '#07111b';
    ctx.fillText(label, ox + x1 + 6, labelY + 14);
  }
}

function drawFrame(msg) {
  const img = $('#video');
  latestBoxes = msg.boxes || [];
  $('#videoEmpty').classList.add('hidden');
  $('#fps').textContent = `${msg.fps ?? 0} FPS`;

  // Vẽ overlay đúng thời điểm ảnh mới load xong để kích thước naturalWidth/Height luôn khớp (đặc biệt với 320x240 của ESP32-CAM)
  img.onload = renderOverlay;
  img.src = msg.image;
  if (img.complete && img.naturalWidth > 0) {
    renderOverlay();
  }
  if (msg.telemetry) {
    renderTelemetry(msg.telemetry);
  }
}

function renderModels(models = {}) {
  $('#models').innerHTML = Object.entries(models)
    .map(([name, status]) => {
      const isWarn = status.includes('Bypass') || status.includes('Thiếu') || status.includes('Lỗi');
      return `<span class="model-badge ${isWarn ? 'bypass' : ''}"><i></i>${escapeHtml(name)}: ${escapeHtml(status)}</span>`;
    })
    .join('');
}

function setLock(locked, durationSec = null) {
  isLocked = Boolean(locked);
  $('#lockLabel').textContent = isLocked ? 'ĐANG KHÓA' : 'ĐÃ MỞ KHÓA';
  const icon = $('#lockIcon');
  icon.className = 'lock-icon ' + (isLocked ? 'locked' : 'open');
  icon.innerHTML = isLocked ? LOCK_SVG_CLOSED : LOCK_SVG_OPEN;

  if (isLocked) {
    unlockDeadlineMs = 0;
    $('#unlockProgress').style.width = '0%';
  } else {
    const sec = Number(durationSec || $('#unlockDuration').value || 5);
    unlockTotalMs = Math.max(1000, sec * 1000);
    unlockDeadlineMs = Date.now() + unlockTotalMs;
    $('#unlockProgress').style.width = '100%';
    hidePendingVoice();
  }
}

function setEsp(online, telemetry = null) {
  $('#espDot').className = 'dot ' + (online ? 'online' : 'offline');
  if (online) {
    const via = telemetry?.transport === 'websocket' ? ' (WS)' : '';
    $('#espStatus').textContent = `ESP32 trực tuyến${via}`;
  } else {
    $('#espStatus').textContent = 'ESP32 ngoại tuyến';
  }
}

function renderTelemetry(t = {}) {
  const transportMap = {
    websocket: 'ESP32 WebSocket',
    http: 'ESP32 HTTP',
    webcam: 'Webcam',
    none: 'Chưa kết nối',
  };
  const channel = transportMap[t.transport] || t.transport || '—';
  const res = t.resolution ? ` (${t.resolution})` : '';
  const door = t.door || '—';
  const rssi = t.rssi != null ? `${t.rssi} dBm` : '—';
  const ip = t.ip ? `<span>IP: ${escapeHtml(t.ip)}</span>` : '';
  const frames = t.frames_received ? `<span>Frames: ${escapeHtml(t.frames_received)}</span>` : '';
  $('#telemetry').innerHTML =
    `Kênh: ${escapeHtml(channel)}${escapeHtml(res)}` +
    `<span>Cửa: ${escapeHtml(door)}</span>` +
    `<span>Wi-Fi RSSI: ${escapeHtml(rssi)}</span>` +
    ip +
    frames;
}

function addEvent(ev, prepend = false) {
  const list = $('#events');
  const resultText = ev.result || 'Sự kiện';
  const tagClass =
    resultText.includes('giả') || resultText.includes('đột nhập')
      ? 'bad'
      : resultText.includes('Từ chối')
        ? 'warn'
        : '';

  const row = document.createElement('div');
  row.className = 'event';
  const imgSrc = ev.image_path ? `/static/${encodeURI(ev.image_path)}` : '';
  const userText = ev.display_name || ev.user || 'Hệ thống';
  const timeText = new Date(ev.timestamp || Date.now()).toLocaleTimeString('vi-VN');
  const faceScore = typeof ev.face_score === 'number' ? ev.face_score.toFixed(2) : '—';
  const spoofPart = typeof ev.spoof_score === 'number' ? ` · Real ${ev.spoof_score.toFixed(2)}` : '';
  const voicePart = typeof ev.voice_score === 'number' ? ` · Voice ${ev.voice_score.toFixed(2)}` : '';

  row.innerHTML =
    `<img src="${escapeHtml(imgSrc)}" alt="">` +
    `<div><b>${escapeHtml(userText)}</b>` +
    `<small>${escapeHtml(resultText)} · ${escapeHtml(timeText)}<br>Face ${faceScore}${spoofPart}${voicePart}</small></div>` +
    `<span class="tag ${tagClass}">${escapeHtml(resultText)}</span>`;

  if (prepend) {
    list.prepend(row);
  } else {
    list.append(row);
  }
  $('#eventCount').textContent = `${list.children.length} sự kiện`;
}

function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${proto}://${location.host}/ws`);

  socket.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }

    if (msg.type === 'frame') drawFrame(msg);
    if (msg.type === 'status') {
      renderModels(msg.models);
      if (typeof msg.locked === 'boolean') setLock(msg.locked);
    }
    if (msg.type === 'event') addEvent(msg, true);
    if (msg.type === 'lock') setLock(msg.locked, msg.duration_sec);
    if (msg.type === 'voice_pending') {
      const badge = $('#pendingVoice');
      badge.textContent = `Chờ giọng nói: ${msg.user} · ${msg.seconds}s`;
      badge.classList.remove('hidden');
      toast('Khuôn mặt hợp lệ. Hãy bấm "Thu âm xác thực 3s".');
    }
    if (msg.type === 'voice_cleared') hidePendingVoice();
    if (msg.type === 'telemetry') {
      renderTelemetry(msg.telemetry);
      setEsp(Boolean(msg.telemetry?.online), msg.telemetry);
    }
    if (msg.type === 'esp32') {
      setEsp(Boolean(msg.online), msg.telemetry);
      if (msg.telemetry) renderTelemetry(msg.telemetry);
    }
    if (msg.type === 'camera_source') {
      $('#cameraSource').value = msg.source;
      if (msg.url) $('#camUrl').value = msg.url;
    }
    if (msg.type === 'notice' && msg.message) toast(msg.message);
  };

  socket.onclose = () => setTimeout(connectWebSocket, 2000);
}

async function loadMembers() {
  const members = await api('/api/members');
  const container = $('#members');
  if (!members.length) {
    container.innerHTML = '<small class="hint">Chưa có thành viên</small>';
    return;
  }
  container.innerHTML = members
    .map(
      (m) =>
        `<div class="member">
          <span>
            ${escapeHtml(m.display_name)}
            <small>${escapeHtml(m.user_id)} · ${Number(m.face_samples || 0)} ảnh · ${Number(m.voice_samples || 0)} giọng</small>
          </span>
          <button type="button" data-del="${escapeHtml(m.user_id)}">Xóa</button>
        </div>`
    )
    .join('');
}

async function loadData() {
  try {
    const s = await api('/api/status');
    renderModels(s.models);
    setLock(s.locked);
    $('#cameraSource').value = s.camera_source;
    $('#mode').value = s.mode;
    $('#camUrl').value = s.camera_url;

    const t = s.thresholds || {};
    for (const [id, key] of [
      ['faceThreshold', 'face_threshold'],
      ['spoofThreshold', 'spoof_threshold'],
      ['voiceThreshold', 'voice_threshold'],
      ['unlockDuration', 'unlock_duration'],
    ]) {
      if (t[key] != null) $('#' + id).value = t[key];
      updateOutput($('#' + id));
    }

    setEsp(Boolean(s.telemetry?.online), s.telemetry);
    renderTelemetry(s.telemetry);

    const events = await api('/api/events');
    $('#events').innerHTML = '';
    events.reverse().forEach((ev) => addEvent(ev, true));
    await loadMembers();
  } catch (err) {
    toast(err.message);
  }
}

function updateOutput(el) {
  const outId = el.id.replace('Threshold', 'Out').replace('unlockDuration', 'durationOut');
  const out = $('#' + outId);
  if (!out) return;
  out.textContent = el.id === 'unlockDuration' ? `${el.value} giây` : Number(el.value).toFixed(2);
}

async function saveThresholds() {
  const body = {
    face_threshold: Number($('#faceThreshold').value),
    spoof_threshold: Number($('#spoofThreshold').value),
    voice_threshold: Number($('#voiceThreshold').value),
    unlock_duration: Number($('#unlockDuration').value),
  };
  try {
    await api('/api/thresholds', { method: 'POST', body: JSON.stringify(body) });
  } catch (err) {
    toast(err.message);
  }
}

async function captureAudio3s() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('Trình duyệt không hỗ trợ microphone (cần localhost hoặc HTTPS)');
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];

  source.connect(processor);
  processor.connect(ctx.destination);
  processor.onaudioprocess = (event) => {
    chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
  };

  try {
    await new Promise((resolve) => setTimeout(resolve, 3000));
  } finally {
    processor.disconnect();
    source.disconnect();
    stream.getTracks().forEach((track) => track.stop());
  }

  const totalLen = chunks.reduce((sum, arr) => sum + arr.length, 0);
  const merged = new Float32Array(totalLen);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }

  const sampleRate = ctx.sampleRate;
  await ctx.close();
  return { samples: Array.from(merged), sample_rate: sampleRate };
}

// ---------- Event Listeners ----------
document.querySelectorAll('.tab').forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === btn));
    document.querySelectorAll('.tab-content').forEach((x) => x.classList.add('hidden'));
    $('#tab-' + btn.dataset.tab).classList.remove('hidden');
  };
});

$('#reloadModels').onclick = async () => {
  try {
    const res = await api('/api/models/reload', { method: 'POST' });
    renderModels(res.models);
    await loadMembers();
    toast('Đã nạp lại trạng thái Model');
  } catch (err) {
    toast(err.message);
  }
};

$('#cameraSource').onchange = async (event) => {
  try {
    const res = await api('/api/camera', {
      method: 'POST',
      body: JSON.stringify({ source: event.target.value, url: $('#camUrl').value.trim() }),
    });
    if (res.telemetry) renderTelemetry(res.telemetry);
    toast('Đã đổi nguồn camera');
  } catch (err) {
    toast(err.message);
  }
};

$('#camUrl').onchange = () => $('#cameraSource').dispatchEvent(new Event('change'));

$('#mode').onchange = async (event) => {
  try {
    await api('/api/mode', { method: 'POST', body: JSON.stringify({ mode: event.target.value }) });
    if (event.target.value === 'FACE_ONLY') hidePendingVoice();
    toast('Đã cập nhật chế độ bảo mật');
  } catch (err) {
    toast(err.message);
  }
};

$('#remoteUnlock').onclick = async () => {
  try {
    await api('/api/unlock', { method: 'POST' });
    toast('Đã gửi lệnh mở khóa');
  } catch (err) {
    toast(err.message);
  }
};

$('#emergencyLock').onclick = async () => {
  try {
    await api('/api/lock', { method: 'POST' });
    toast('Đã khóa cửa khẩn cấp');
  } catch (err) {
    toast(err.message);
  }
};

document.querySelectorAll('.slider-group input').forEach((slider) => {
  slider.oninput = () => {
    updateOutput(slider);
    clearTimeout(thresholdTimer);
    thresholdTimer = setTimeout(saveThresholds, 250);
  };
});

$('#enrollForm').onsubmit = async (event) => {
  event.preventDefault();
  try {
    const res = await api('/api/enroll/face', { method: 'POST', body: new FormData(event.target) });
    toast(`Đã đăng ký khuôn mặt (${res.samples} mẫu)`);
    event.target.reset();
    await loadMembers();
  } catch (err) {
    toast(err.message);
  }
};

$('#members').onclick = async (event) => {
  const id = event.target?.dataset?.del;
  if (!id) return;
  if (!confirm(`Xóa thành viên ${id}?`)) return;
  try {
    await api('/api/members/' + encodeURIComponent(id), { method: 'DELETE' });
    await loadMembers();
    toast(`Đã xóa thành viên ${id}`);
  } catch (err) {
    toast(err.message);
  }
};

$('#currentFaceSample').onclick = async () => {
  const form = $('#enrollForm');
  const user_id = form.elements.user_id.value.trim();
  const display_name = form.elements.display_name.value.trim();
  if (!user_id) {
    toast('Nhập mã người dùng trước');
    return;
  }
  try {
    const res = await api('/api/enroll/face/current', {
      method: 'POST',
      body: JSON.stringify({ user_id, display_name }),
    });
    toast(`Đã nối mẫu ${res.samples} cho ${res.user_id}`);
    await loadMembers();
  } catch (err) {
    toast(err.message);
  }
};

$('#enrollVoiceSample').onclick = async () => {
  const form = $('#enrollForm');
  const user_id = form.elements.user_id.value.trim();
  if (!user_id) {
    toast('Nhập mã người dùng trước');
    return;
  }
  const btn = $('#enrollVoiceSample');
  btn.disabled = true;
  try {
    btn.textContent = 'Đang thu âm 3 giây…';
    voiceEnrollmentSamples.push(await captureAudio3s());
    $('#voiceSampleCount').textContent = `${voiceEnrollmentSamples.length}/3 mẫu`;

    if (voiceEnrollmentSamples.length === 3) {
      const display_name = form.elements.display_name.value.trim();
      const res = await api('/api/enroll/voice', {
        method: 'POST',
        body: JSON.stringify({
          user_id,
          display_name,
          sample_rate: voiceEnrollmentSamples[0].sample_rate,
          samples: voiceEnrollmentSamples.map((s) => s.samples),
        }),
      });
      toast(`Đã lưu ${res.samples} mẫu giọng cho ${user_id}`);
      voiceEnrollmentSamples = [];
      $('#voiceSampleCount').textContent = '0/3 mẫu';
      await loadMembers();
    } else {
      toast(`Đã ghi mẫu ${voiceEnrollmentSamples.length}/3`);
    }
  } catch (err) {
    voiceEnrollmentSamples = [];
    $('#voiceSampleCount').textContent = '0/3 mẫu';
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Ghi mẫu giọng nói 3 giây';
  }
};

$('#recordVoice').onclick = async () => {
  const btn = $('#recordVoice');
  btn.disabled = true;
  btn.textContent = 'Đang thu âm 3s…';
  try {
    const audio = await captureAudio3s();
    const res = await api('/api/verify-voice', {
      method: 'POST',
      body: JSON.stringify(audio),
    });
    if (res.ok) {
      hidePendingVoice();
      toast(`Xác thực giọng nói đạt (${Number(res.score || 0).toFixed(2)})`);
    } else {
      toast(res.error || `Không khớp giọng nói (${Number(res.score || 0).toFixed(2)})`);
    }
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Thu âm xác thực 3s';
  }
};

window.addEventListener('resize', renderOverlay);

// Cập nhật mượt thanh tiến trình đếm ngược mở khóa (100% -> 0%)
setInterval(() => {
  if (isLocked || !unlockDeadlineMs) return;
  const remaining = Math.max(0, unlockDeadlineMs - Date.now());
  const pct = Math.max(0, Math.min(100, (remaining / unlockTotalMs) * 100));
  $('#unlockProgress').style.width = `${pct.toFixed(1)}%`;
}, 100);

connectWebSocket();
loadData();
