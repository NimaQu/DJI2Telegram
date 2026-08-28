const elements = Object.fromEntries([
  "token", "connectButton", "connectionBadge", "moduleState", "moduleIdentity",
  "signalDbm", "signalQuality", "signalBars", "signalMeta",
  "callState", "callNumber", "callFrontend", "audioState", "telegramState", "audioHint",
  "phoneNumber", "startCallButton", "answerButton", "hangupButton", "muteButton",
  "diagnosticButton", "refreshButton", "smsNumber", "smsText", "smsLength", "sendSmsButton", "smsResult",
  "refreshSmsButton", "smsInbox", "eventLog", "clearEventsButton",
].map((id) => [id, document.getElementById(id)]));

let token = "";
let currentCall = null;
let eventAbort = null;
let eventLoopGeneration = 0;
let audioContext = null;
let mediaStream = null;
let captureNode = null;
let playbackNode = null;
let audioSocket = null;
let muted = false;
let diagnosticSession = null;
let audioPreparation = null;
let browserCaptureFrames = 0;
const intentionallyClosedSockets = new WeakSet();
const browserAudioSupported = Boolean(window.isSecureContext && navigator.mediaDevices?.getUserMedia);
const TOKEN_STORAGE_KEY = "qdc507.gateway.bearer-token.v1";
const MICROPHONE_REQUEST_TIMEOUT_MS = 12000;
const BROWSER_CAPTURE_START_TIMEOUT_MS = 2000;

class APIError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "APIError";
    this.status = status;
  }
}

function loadSavedToken() {
  try { return window.localStorage.getItem(TOKEN_STORAGE_KEY)?.trim() || ""; }
  catch (_) { return ""; }
}

function saveToken(value) {
  try { window.localStorage.setItem(TOKEN_STORAGE_KEY, value); }
  catch (_) { log("浏览器拒绝保存 Token；本次连接仍然有效"); }
}

function forgetSavedToken() {
  try { window.localStorage.removeItem(TOKEN_STORAGE_KEY); }
  catch (_) { /* unavailable browser storage */ }
}

function setBadge(text, kind = "neutral") {
  elements.connectionBadge.textContent = text;
  elements.connectionBadge.className = `badge ${kind}`;
}

function log(message, payload = null) {
  const stamp = new Date().toLocaleTimeString();
  const suffix = payload === null ? "" : ` ${JSON.stringify(payload)}`;
  const line = `[${stamp}] ${message}${suffix}`;
  elements.eventLog.textContent = elements.eventLog.textContent === "等待连接…"
    ? line : `${line}\n${elements.eventLog.textContent}`;
}

async function api(path, options = {}) {
  if (!token) throw new Error("请先输入并连接 Bearer Token");
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  let body = null;
  try { body = await response.json(); } catch (_) { body = null; }
  if (!response.ok) throw new APIError(body?.detail || `HTTP ${response.status}`, response.status);
  return body;
}

function renderCall(call) {
  currentCall = call;
  if (!call) {
    elements.callState.textContent = "空闲";
    elements.callNumber.textContent = "—";
    elements.callFrontend.textContent = "web";
  } else {
    elements.callState.textContent = call.state;
    elements.callNumber.textContent = call.cellular_number || "号码未知";
    elements.callFrontend.textContent = call.frontend || "telegram";
  }
  const isWeb = call?.frontend === "web";
  const incoming = isWeb && call.direction === "inbound_cellular";
  elements.startCallButton.disabled = Boolean(call) || Boolean(diagnosticSession) || !browserAudioSupported;
  elements.answerButton.disabled = !(incoming && call.state === "ringing_cellular" && browserAudioSupported);
  elements.hangupButton.disabled = !isWeb;
  elements.diagnosticButton.disabled = Boolean(call) || !browserAudioSupported;
}

function renderSignal(signal) {
  const dbm = Number.isFinite(signal?.dbm) ? signal.dbm : null;
  const level = Math.max(0, Math.min(5, Number(signal?.bars) || 0));
  const labels = ["未采样", "极弱", "较弱", "一般", "良好", "很强"];
  elements.signalDbm.textContent = dbm === null ? "— dBm" : `${dbm} dBm`;
  elements.signalQuality.textContent = labels[level];
  elements.signalBars.dataset.level = String(level);
  elements.signalBars.setAttribute(
    "aria-label",
    dbm === null ? "蜂窝信号未知" : `蜂窝信号 ${dbm} dBm，${labels[level]}`,
  );
  if (dbm === null) {
    elements.signalMeta.textContent = signal?.error ? "AT+CSQ · 暂不可用" : "AT+CSQ · —";
    return;
  }
  const ber = Number.isInteger(signal.ber) ? ` · BER ${signal.ber}` : "";
  elements.signalMeta.textContent = `CSQ ${signal.rssi}/31${ber}`;
}

async function refreshStatus() {
  const [status, module, call] = await Promise.all([
    api("/api/v1/status"),
    api("/api/v1/module"),
    api("/api/v1/calls/current"),
  ]);
  elements.moduleState.textContent = status.module_state || (module.connected ? "connected" : "disconnected");
  elements.moduleIdentity.textContent = module.identity || "—";
  renderSignal(module.connected ? module.signal : null);
  elements.telegramState.textContent = (
    `User ${status.telegram_state || "disabled"} · `
    + `Bot ${status.telegram_bot_state || "disabled"}`
  );
  const mode = status.audio?.mode;
  elements.audioState.textContent = status.audio_diagnostic_active
    ? "诊断模式已连接"
    : (mode ? `${mode} 已连接` : "未连接");
  renderCall(call);
}

async function refreshSms() {
  const response = await api("/api/v1/sms?limit=20");
  elements.smsInbox.replaceChildren();
  if (!response.items.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "暂无短信。";
    elements.smsInbox.append(empty);
    return;
  }
  for (const message of response.items) {
    const item = document.createElement("article");
    item.className = "sms-item";
    const head = document.createElement("div");
    head.className = "sms-item-head";
    const sender = document.createElement("span");
    sender.textContent = message.sender || "未知号码";
    const time = document.createElement("time");
    time.textContent = message.timestamp ? new Date(message.timestamp).toLocaleString() : "";
    const body = document.createElement("p");
    body.textContent = message.body || "";
    head.append(sender, time);
    item.append(head, body);
    elements.smsInbox.append(item);
  }
}

async function consumeEvents(generation) {
  eventAbort = new AbortController();
  const response = await fetch("/api/v1/events", {
    headers: { Authorization: `Bearer ${token}` },
    signal: eventAbort.signal,
  });
  if (!response.ok || !response.body) {
    throw new APIError(`事件流连接失败：HTTP ${response.status}`, response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (generation === eventLoopGeneration) {
    const { value, done } = await reader.read();
    if (done) throw new Error("事件流连接已关闭");
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      const eventLine = block.split("\n").find((line) => line.startsWith("event:"));
      const dataLine = block.split("\n").find((line) => line.startsWith("data:"));
      if (!eventLine || !dataLine) continue;
      const eventName = eventLine.slice(6).trim();
      let payload = dataLine.slice(5).trim();
      try { payload = JSON.parse(payload); } catch (_) { /* keep raw text */ }
      log(eventName, payload);
      if (eventName.startsWith("call.") || eventName.startsWith("audio.") || eventName.startsWith("module.")) {
        refreshStatus().catch((error) => log("状态刷新失败", { error: error.message }));
      }
      if (eventName.startsWith("sms.")) {
        refreshSms().catch((error) => log("短信刷新失败", { error: error.message }));
      }
    }
  }
}

function startEvents() {
  eventLoopGeneration += 1;
  const generation = eventLoopGeneration;
  if (eventAbort) eventAbort.abort();
  void runEventLoop(generation);
}

async function runEventLoop(generation) {
  let reconnecting = false;
  let delayMs = 750;
  while (generation === eventLoopGeneration && token) {
    try {
      await consumeEvents(generation);
      return;
    } catch (error) {
      if (generation !== eventLoopGeneration || error.name === "AbortError") return;
      if (error instanceof APIError && error.status === 401) {
        forgetSavedToken();
        token = "";
        setBadge("Token 已失效", "error");
        log("事件流认证失败，请重新输入 Token");
        return;
      }
      if (!reconnecting) {
        reconnecting = true;
        setBadge("事件流重连中", "neutral");
        log("事件流断开，正在自动重连", { error: error.message });
      }
      await new Promise((resolve) => window.setTimeout(resolve, delayMs));
      delayMs = Math.min(delayMs * 2, 5000);
      if (generation !== eventLoopGeneration || !token) return;
      try {
        await refreshStatus();
        await refreshSms();
        setBadge("API 已连接", "ok");
        log("API 状态已恢复，等待事件流");
        reconnecting = false;
        delayMs = 750;
      } catch (_) { /* retry the event stream after the backoff */ }
    }
  }
}

function browserAudioIsReusable() {
  if (!audioContext || audioContext.state === "closed" || !mediaStream || !captureNode || !playbackNode) {
    return false;
  }
  const tracks = mediaStream.getAudioTracks();
  return tracks.length > 0 && tracks.every((track) => track.readyState === "live");
}

async function ensureBrowserAudio() {
  if (audioPreparation) return await audioPreparation;
  audioPreparation = prepareBrowserAudio();
  try {
    await audioPreparation;
  } finally {
    audioPreparation = null;
  }
}

async function prepareBrowserAudio() {
  if (!browserAudioSupported) {
    throw new Error("浏览器麦克风要求 HTTPS，或通过 SSH 转发后使用 localhost");
  }
  if (browserAudioIsReusable()) {
    try {
      const previousFrameCount = browserCaptureFrames;
      mediaStream.getAudioTracks().forEach((track) => { track.enabled = true; });
      captureNode.port.postMessage({ type: "enabled", value: !muted });
      await audioContext.resume();
      await waitForBrowserCaptureFrame(previousFrameCount);
      elements.muteButton.disabled = false;
      elements.audioState.textContent = "麦克风已就绪";
      log("复用已授权的浏览器麦克风");
      return;
    } catch (_) {
      log("浏览器麦克风复用失活，正在重建");
      await releaseBrowserAudioResources();
    }
  }
  if (audioContext || mediaStream || captureNode || playbackNode) {
    await releaseBrowserAudioResources();
  }
  elements.audioState.textContent = "等待麦克风权限…";
  log("正在请求浏览器麦克风权限");
  const nextContext = new AudioContext({ latencyHint: "interactive" });
  let nextStream = null;
  try {
    await nextContext.audioWorklet.addModule("/web/audio-worklet.js");
    nextStream = await getUserMediaWithTimeout({
      audio: {
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
      video: false,
    });
    const source = nextContext.createMediaStreamSource(nextStream);
    const nextCapture = new AudioWorkletNode(nextContext, "qdc507-capture");
    const nextPlayback = new AudioWorkletNode(nextContext, "qdc507-playback");
    const silent = nextContext.createGain();
    silent.gain.value = 0;
    source.connect(nextCapture).connect(silent).connect(nextContext.destination);
    nextPlayback.connect(nextContext.destination);
    nextCapture.port.onmessage = (event) => {
      if (!(event.data instanceof ArrayBuffer)) return;
      browserCaptureFrames += 1;
      if (audioSocket?.readyState === WebSocket.OPEN) audioSocket.send(event.data);
    };
    const previousFrameCount = browserCaptureFrames;
    nextCapture.port.postMessage({ type: "enabled", value: !muted });
    await nextContext.resume();
    await waitForBrowserCaptureFrame(previousFrameCount);
    audioContext = nextContext;
    mediaStream = nextStream;
    captureNode = nextCapture;
    playbackNode = nextPlayback;
    elements.muteButton.disabled = false;
    elements.audioState.textContent = "麦克风已就绪";
    log("浏览器麦克风已就绪");
  } catch (error) {
    if (nextStream) nextStream.getTracks().forEach((track) => track.stop());
    try { await nextContext.close(); } catch (_) { /* already closed */ }
    elements.audioState.textContent = "麦克风不可用";
    throw new Error(microphoneErrorMessage(error));
  }
}

async function waitForBrowserCaptureFrame(previousFrameCount) {
  const deadline = performance.now() + BROWSER_CAPTURE_START_TIMEOUT_MS;
  while (performance.now() < deadline) {
    if (browserCaptureFrames > previousFrameCount) return;
    await new Promise((resolve) => window.setTimeout(resolve, 25));
  }
  throw new Error("浏览器麦克风采集链路没有产生 PCM 音频帧");
}

async function getUserMediaWithTimeout(constraints) {
  let expired = false;
  let timer = null;
  const request = navigator.mediaDevices.getUserMedia(constraints);
  request.then((stream) => {
    if (expired) stream.getTracks().forEach((track) => track.stop());
  }).catch(() => {});
  const timeout = new Promise((_, reject) => {
    timer = window.setTimeout(() => {
      expired = true;
      reject(new Error("麦克风权限请求超时，请检查浏览器地址栏中的麦克风权限"));
    }, MICROPHONE_REQUEST_TIMEOUT_MS);
  });
  try {
    return await Promise.race([request, timeout]);
  } finally {
    if (timer !== null) window.clearTimeout(timer);
  }
}

function microphoneErrorMessage(error) {
  if (error?.name === "NotAllowedError") {
    return "麦克风权限被拒绝，请在浏览器地址栏中允许麦克风后重试";
  }
  if (error?.name === "NotFoundError") return "没有找到可用的麦克风";
  if (error?.name === "NotReadableError") return "麦克风正被其他应用占用或无法读取";
  return error?.message || "浏览器麦克风启动失败";
}

async function connectAudio(sessionId, ticketPath, socketPath) {
  const issued = await api(ticketPath, { method: "POST" });
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${scheme}//${location.host}${socketPath}`;
  await new Promise((resolve, reject) => {
    const socket = new WebSocket(url, [issued.subprotocol, `ticket.${issued.ticket}`]);
    socket.binaryType = "arraybuffer";
    audioSocket = socket;
    let ready = false;
    const timer = setTimeout(() => {
      if (!ready) {
        socket.close();
        reject(new Error("音频 WebSocket 就绪超时"));
      }
    }, 15000);
    socket.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        playbackNode?.port.postMessage(event.data, [event.data]);
        return;
      }
      try {
        const message = JSON.parse(event.data);
        if (message.type === "ready") {
          ready = true;
          clearTimeout(timer);
          elements.audioState.textContent = "web 已连接";
          log("双向音频已连接", message.format);
          resolve();
        } else if (message.type === "error") {
          reject(new Error(message.detail || "音频会话失败"));
        }
      } catch (_) { /* ignore unknown control messages */ }
    };
    socket.onerror = () => {
      clearTimeout(timer);
      if (!ready) reject(new Error("音频 WebSocket 连接失败"));
    };
    socket.onclose = () => {
      clearTimeout(timer);
      const wasCurrentSocket = audioSocket === socket;
      if (wasCurrentSocket) {
        audioSocket = null;
        resetMuteState();
        elements.audioState.textContent = "未连接";
        diagnosticSession = null;
        elements.diagnosticButton.textContent = "仅测试双向音频";
        renderCall(currentCall);
        void parkBrowserAudio();
      }
      if (!intentionallyClosedSockets.has(socket)) {
        log("音频 WebSocket 已断开");
        refreshStatus().catch(() => {});
      }
      if (!ready) reject(new Error("音频 WebSocket 在就绪前关闭"));
    };
  });
}

async function parkBrowserAudio() {
  // Keep the capture graph hot between calls. Some browsers resume playback
  // after AudioContext.suspend(), but leave the microphone worklet stalled.
  // Frames are discarded by the port handler while no WebSocket is open.
  captureNode?.port.postMessage({ type: "enabled", value: true });
  if (mediaStream) {
    mediaStream.getAudioTracks().forEach((track) => {
      if (track.readyState === "live") track.enabled = true;
    });
  }
}

function resetMuteState() {
  muted = false;
  elements.muteButton.textContent = "麦克风静音";
}

async function releaseBrowserAudioResources() {
  if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
  mediaStream = null;
  captureNode = null;
  playbackNode = null;
  if (audioContext) {
    try { await audioContext.close(); } catch (_) { /* already closed */ }
  }
  audioContext = null;
}

async function closeBrowserAudio({ releaseMedia = false } = {}) {
  if (audioSocket) {
    const socket = audioSocket;
    audioSocket = null;
    intentionallyClosedSockets.add(socket);
    try { socket.close(1000, "client cleanup"); } catch (_) { /* already closed */ }
  }
  if (releaseMedia) await releaseBrowserAudioResources();
  else {
    resetMuteState();
    await parkBrowserAudio();
  }
  elements.audioState.textContent = "未连接";
  elements.muteButton.disabled = true;
  diagnosticSession = null;
  elements.diagnosticButton.textContent = "仅测试双向音频";
  renderCall(currentCall);
}

async function beginOutboundCall() {
  const number = elements.phoneNumber.value.trim();
  if (!number) throw new Error("请输入电话号码");
  if (!window.confirm(`确认通过 QDC507 拨打 ${number}？`)) return;
  await ensureBrowserAudio();
  let call = null;
  try {
    call = await api("/api/v1/calls/start", {
      method: "POST",
      body: JSON.stringify({ number, frontend: "web" }),
    });
    renderCall(call);
    await connectAudio(
      call.id,
      `/api/v1/calls/${encodeURIComponent(call.id)}/audio-ticket`,
      `/api/v1/calls/${encodeURIComponent(call.id)}/audio`,
    );
    await refreshStatus();
  } catch (error) {
    if (call?.id) {
      try { await api(`/api/v1/calls/${encodeURIComponent(call.id)}/hangup`, { method: "POST" }); } catch (_) { /* best effort */ }
    }
    await closeBrowserAudio();
    throw error;
  }
}

async function answerIncomingCall() {
  if (!currentCall || currentCall.frontend !== "web") throw new Error("当前没有网页来电");
  await ensureBrowserAudio();
  try {
    if (!audioSocket) await connectAudio(
      currentCall.id,
      `/api/v1/calls/${encodeURIComponent(currentCall.id)}/audio-ticket`,
      `/api/v1/calls/${encodeURIComponent(currentCall.id)}/audio`,
    );
    const call = await api(`/api/v1/calls/${encodeURIComponent(currentCall.id)}/answer`, { method: "POST" });
    renderCall(call);
  } catch (error) {
    await closeBrowserAudio();
    throw error;
  }
}

async function hangupCall() {
  if (!currentCall) return;
  const callId = currentCall.id;
  await api(`/api/v1/calls/${encodeURIComponent(callId)}/hangup`, { method: "POST" });
  await closeBrowserAudio();
  renderCall(null);
}

async function toggleAudioDiagnostic() {
  if (diagnosticSession || audioSocket) {
    await closeBrowserAudio();
    await refreshStatus();
    return;
  }
  await ensureBrowserAudio();
  try {
    diagnosticSession = await api("/api/v1/audio/diagnostic/start", { method: "POST" });
    elements.diagnosticButton.textContent = "停止音频测试";
    renderCall(currentCall);
    await connectAudio(
      diagnosticSession.id,
      `/api/v1/audio/diagnostic/${encodeURIComponent(diagnosticSession.id)}/ticket`,
      `/api/v1/audio/diagnostic/${encodeURIComponent(diagnosticSession.id)}`,
    );
    await refreshStatus();
  } catch (error) {
    await closeBrowserAudio();
    throw error;
  }
}

async function sendSms() {
  const to = elements.smsNumber.value.trim();
  const text = elements.smsText.value;
  if (!to || !text) throw new Error("请输入收件号码和短信内容");
  if (!window.confirm(`确认向 ${to} 发送 ${text.length} 个字符？`)) return;
  elements.smsResult.textContent = "正在发送…";
  const result = await api("/api/v1/sms/send", {
    method: "POST",
    body: JSON.stringify({ to, text }),
  });
  elements.smsResult.textContent = `模块已接受：${result.result?.segments ?? 0} 段`;
  log("短信发送完成", { to, segments: result.result?.segments });
}

function guard(action) {
  return async () => {
    try { await action(); }
    catch (error) {
      log("操作失败", { error: error.message });
      if (!token || (error instanceof APIError && error.status === 401)) {
        setBadge(error.message, "error");
      }
    }
  };
}

async function connectGateway({ automatic = false } = {}) {
  const candidate = elements.token.value.trim();
  if (!candidate) throw new Error("请输入 Bearer Token");
  token = candidate;
  elements.connectButton.disabled = true;
  elements.connectButton.textContent = automatic ? "自动连接中…" : "连接中…";
  setBadge(automatic ? "正在自动连接" : "正在连接", "neutral");
  try {
    await refreshStatus();
  } catch (error) {
    token = "";
    if (error instanceof APIError && error.status === 401) forgetSavedToken();
    throw error;
  } finally {
    elements.connectButton.disabled = false;
    elements.connectButton.textContent = "连接网关";
  }
  saveToken(candidate);
  refreshSms().catch((error) => log("短信列表不可用", { error: error.message }));
  setBadge("API 已连接", "ok");
  log(automatic ? "已使用保存的 Token 自动连接" : "API 已连接");
  startEvents();
}

elements.connectButton.addEventListener("click", guard(connectGateway));
elements.refreshButton.addEventListener("click", guard(refreshStatus));
elements.refreshSmsButton.addEventListener("click", guard(refreshSms));
elements.startCallButton.addEventListener("click", guard(beginOutboundCall));
elements.answerButton.addEventListener("click", guard(answerIncomingCall));
elements.hangupButton.addEventListener("click", guard(hangupCall));
elements.diagnosticButton.addEventListener("click", guard(toggleAudioDiagnostic));
elements.sendSmsButton.addEventListener("click", guard(sendSms));
elements.clearEventsButton.addEventListener("click", () => { elements.eventLog.textContent = ""; });
elements.muteButton.addEventListener("click", () => {
  muted = !muted;
  captureNode?.port.postMessage({ type: "enabled", value: !muted });
  elements.muteButton.textContent = muted ? "取消静音" : "麦克风静音";
});
elements.smsText.addEventListener("input", () => {
  elements.smsLength.textContent = `${elements.smsText.value.length} 字符`;
});
window.addEventListener("beforeunload", () => {
  eventLoopGeneration += 1;
  if (audioSocket) {
    intentionallyClosedSockets.add(audioSocket);
    audioSocket.close();
  }
  if (eventAbort) eventAbort.abort();
  if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
  if (audioContext) void audioContext.close();
});

renderCall(null);
if (!browserAudioSupported) {
  elements.audioHint.textContent = "当前页面不是浏览器认可的安全来源，麦克风已禁用。状态和短信仍可在 LAN 使用；电话音频请使用 HTTPS，或 SSH 转发到 localhost。";
}

const savedToken = loadSavedToken();
if (savedToken) {
  elements.token.value = savedToken;
  connectGateway({ automatic: true }).catch((error) => {
    log("自动连接失败", { error: error.message });
    setBadge(error.message, "error");
  });
}
