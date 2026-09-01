"use strict";

const MAX_RECORDING_MS = 30_000;
const REQUEST_TIMEOUT_MS = 200_000;
const MUSIC_REQUEST_TIMEOUT_MS = 20_000;

const states = Object.freeze({
  IDLE: "idle",
  STARTING: "starting",
  RECORDING: "recording",
  PROCESSING: "processing",
  SPEAKING: "speaking",
});

const elements = {
  micButton: document.querySelector("#micButton"),
  speakerStage: document.querySelector("#speakerStage"),
  statusTitle: document.querySelector("#statusTitle"),
  statusDetail: document.querySelector("#statusDetail"),
  recordingTimer: document.querySelector("#recordingTimer"),
  stopAudioButton: document.querySelector("#stopAudioButton"),
  messages: document.querySelector("#messages"),
  textForm: document.querySelector("#textForm"),
  textInput: document.querySelector("#textInput"),
  sendButton: document.querySelector("#sendButton"),
  resetButton: document.querySelector("#resetButton"),
  modeBadge: document.querySelector("#modeBadge"),
  modeLabel: document.querySelector("#modeLabel"),
  privacyCard: document.querySelector(".privacy-card"),
  privacyTitle: document.querySelector("#privacyTitle"),
  privacyText: document.querySelector("#privacyText"),
  disclosure: document.querySelector("#voiceDisclosure"),
  stationSelect: document.querySelector("#stationSelect"),
  musicState: document.querySelector("#musicState"),
  musicPlayButton: document.querySelector("#musicPlayButton"),
  musicPauseButton: document.querySelector("#musicPauseButton"),
  musicStopButton: document.querySelector("#musicStopButton"),
  musicVolume: document.querySelector("#musicVolume"),
  musicVolumeValue: document.querySelector("#musicVolumeValue"),
  memoryList: document.querySelector("#memoryList"),
  refreshMemoriesButton: document.querySelector("#refreshMemoriesButton"),
  clearMemoriesButton: document.querySelector("#clearMemoriesButton"),
};

let currentState = states.IDLE;
let mediaRecorder = null;
let mediaStream = null;
let recordedChunks = [];
let recordingStartedAt = 0;
let timerInterval = null;
let discardRecording = false;
let responseAudio = null;
let responseAudioUrl = null;
let responseAudioCancel = null;
let activeSpeechCancel = null;
let requestToken = "";
let currentProviderId = "mock";

let sessionId = createSessionId();

class MusicPlayer {
  constructor(onChange, onError) {
    this.audio = new Audio();
    this.audio.preload = "none";
    this.audio.volume = 0.45;
    this.stations = new Map();
    this.currentStationId = "";
    this.state = "unavailable";
    this.onChange = onChange;
    this.onError = onError;

    this.audio.addEventListener("playing", () => {
      this.state = "playing";
      this.emit();
    });
    this.audio.addEventListener("waiting", () => {
      if (this.currentStationId) {
        this.state = "loading";
        this.emit();
      }
    });
    this.audio.addEventListener("pause", () => {
      if (this.currentStationId && this.state !== "stopped" && this.state !== "error") {
        this.state = "paused";
        this.emit();
      }
    });
    this.audio.addEventListener("ended", () => {
      this.state = "stopped";
      this.emit();
    });
    this.audio.addEventListener("error", () => {
      if (!this.currentStationId) return;
      this.state = "error";
      this.emit();
      this.onError?.("电台暂时无法播放，请稍后重试或选择其他电台。");
    });
  }

  loadStations(rawStations) {
    const nextStations = new Map();
    for (const raw of Array.isArray(rawStations) ? rawStations : []) {
      const id = typeof raw?.id === "string" ? raw.id.trim() : "";
      const name = typeof raw?.name === "string" ? raw.name.trim() : "";
      const rawUrl = raw?.stream_url || raw?.play_url || raw?.url;
      if (!id || !name || typeof rawUrl !== "string") continue;
      try {
        const parsedUrl = new URL(rawUrl, window.location.origin);
        if (parsedUrl.protocol !== "http:" && parsedUrl.protocol !== "https:") continue;
        nextStations.set(id, Object.freeze({
          id,
          name,
          description: typeof raw.description === "string" ? raw.description : "",
          streamUrl: parsedUrl.href,
        }));
      } catch {
        // Ignore malformed entries even when they came from the local server.
      }
    }
    this.stations = nextStations;
    if (!this.stations.has(this.currentStationId)) this.stop();
    this.state = this.stations.size ? "stopped" : "unavailable";
    this.emit();
  }

  hasStation(stationId) {
    return this.stations.has(stationId);
  }

  async play(stationId) {
    const station = this.stations.get(stationId);
    if (!station) throw new Error("请求的电台不在本地白名单中。");
    if (this.currentStationId !== stationId) {
      this.audio.pause();
      this.audio.src = station.streamUrl;
      this.currentStationId = stationId;
    }
    this.state = "loading";
    this.emit();
    try {
      await this.audio.play();
      this.state = "playing";
      this.emit();
    } catch (error) {
      this.state = "error";
      this.emit();
      throw new Error(error?.name === "NotAllowedError"
        ? "浏览器阻止了自动播放，请手动点击电台的播放按钮。"
        : "电台连接失败，请稍后重试。");
    }
  }

  pause() {
    if (!this.currentStationId) return;
    this.audio.pause();
    this.state = "paused";
    this.emit();
  }

  async resume() {
    if (!this.currentStationId) {
      const firstStation = this.stations.keys().next().value;
      if (!firstStation) throw new Error("还没有可用的白名单电台。");
      await this.play(firstStation);
      return;
    }
    await this.play(this.currentStationId);
  }

  stop() {
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.currentStationId = "";
    this.state = this.stations.size ? "stopped" : "unavailable";
    this.emit();
  }

  setVolume(value) {
    const normalized = Math.min(1, Math.max(0, Number(value)));
    if (!Number.isFinite(normalized)) return;
    this.audio.volume = normalized;
    this.emit();
  }

  getStatus() {
    return {
      state: this.state,
      stationId: this.currentStationId,
      station: this.stations.get(this.currentStationId) || null,
      volume: this.audio.volume,
    };
  }

  emit() {
    this.onChange?.(this.getStatus());
  }
}

class AudioFocusManager {
  constructor(player) {
    this.player = player;
    this.interactionActive = false;
    this.resumeAfterInteraction = false;
    this.autoResumeAllowed = false;
    this.deferredAction = null;
  }

  beginInteraction() {
    if (this.interactionActive) return;
    this.interactionActive = true;
    const status = this.player.getStatus();
    this.resumeAfterInteraction = status.state === "playing" || status.state === "loading";
    this.autoResumeAllowed = this.resumeAfterInteraction;
    this.deferredAction = null;
    if (this.resumeAfterInteraction) this.player.pause();
  }

  cancelAutoResume() {
    if (this.interactionActive) this.autoResumeAllowed = false;
  }

  cancelDeferredPlayback() {
    this.cancelAutoResume();
    this.deferredAction = null;
  }

  defer(action) {
    this.cancelAutoResume();
    this.deferredAction = action;
  }

  async endInteraction() {
    if (!this.interactionActive) return;
    const deferredAction = this.deferredAction;
    const shouldResume = this.resumeAfterInteraction && this.autoResumeAllowed;
    this.interactionActive = false;
    this.resumeAfterInteraction = false;
    this.autoResumeAllowed = false;
    this.deferredAction = null;

    if (deferredAction) {
      if (deferredAction.type === "music.play") await this.player.play(deferredAction.stationId);
      else if (deferredAction.type === "music.resume") await this.player.resume();
    } else if (shouldResume) {
      await this.player.resume();
    }
  }
}

const musicPlayer = new MusicPlayer(updateMusicUi, showMusicError);
const audioFocusManager = new AudioFocusManager(musicPlayer);

function createSessionId() {
  let value;
  if (globalThis.crypto?.randomUUID) {
    value = crypto.randomUUID().replaceAll("-", "");
  } else {
    value = `s${Date.now()}${Math.random().toString(36).slice(2)}`;
  }
  return value;
}

function setState(next, title, detail) {
  currentState = next;
  elements.speakerStage.classList.toggle("is-recording", next === states.RECORDING);
  elements.speakerStage.classList.toggle("is-processing", next === states.PROCESSING);
  elements.speakerStage.classList.toggle("is-speaking", next === states.SPEAKING);

  elements.statusTitle.textContent = title;
  elements.statusDetail.textContent = detail;
  elements.micButton.disabled =
    next === states.STARTING || next === states.PROCESSING || next === states.SPEAKING;
  elements.textInput.disabled = next !== states.IDLE;
  elements.sendButton.disabled = next !== states.IDLE;
  elements.resetButton.disabled = next === states.STARTING || next === states.PROCESSING;
  elements.stopAudioButton.disabled = next !== states.SPEAKING;
  elements.recordingTimer.hidden = next !== states.RECORDING;
  elements.micButton.setAttribute(
    "aria-label",
    next === states.RECORDING ? "停止录音" : "开始录音",
  );
  if (typeof musicPlayer !== "undefined") updateMusicUi();
}

function formatDuration(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `00:${String(seconds).padStart(2, "0")}`;
}

function preferredRecorderMime() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4;codecs=mp4a.40.2",
    "audio/mp4",
  ];
  return candidates.find((mime) => MediaRecorder.isTypeSupported(mime)) || "";
}

async function startRecording() {
  if (currentState !== states.IDLE) return;
  if (!navigator.mediaDevices?.getUserMedia || !globalThis.MediaRecorder) {
    showError("当前浏览器不支持麦克风录音，请使用最新版 Chrome 或 Safari。", false);
    return;
  }

  stopResponsePlayback(false);
  audioFocusManager.beginInteraction();
  setState(states.STARTING, "正在连接麦克风", "如出现权限提示，请选择允许");
  try {
    const requestedStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    if (currentState !== states.STARTING) {
      for (const track of requestedStream.getTracks()) track.stop();
      await endAudioFocus();
      return;
    }
    mediaStream = requestedStream;

    const mimeType = preferredRecorderMime();
    mediaRecorder = new MediaRecorder(mediaStream, mimeType ? { mimeType } : undefined);
    recordedChunks = [];
    discardRecording = false;

    mediaRecorder.addEventListener("dataavailable", (event) => {
      if (event.data?.size) recordedChunks.push(event.data);
    });
    mediaRecorder.addEventListener("error", () => {
      releaseMediaStream();
      showError("录音过程中出现错误，请重新尝试。", true);
      void endAudioFocus();
    });
    mediaRecorder.addEventListener("stop", finishRecording, { once: true });

    recordingStartedAt = performance.now();
    mediaRecorder.start(250);
    elements.recordingTimer.textContent = "00:00";
    timerInterval = window.setInterval(() => {
      const elapsed = performance.now() - recordingStartedAt;
      elements.recordingTimer.textContent = formatDuration(elapsed);
      if (elapsed >= MAX_RECORDING_MS) stopRecording();
    }, 200);

    setState(states.RECORDING, "正在听…", "再按一下即可发送");
  } catch (error) {
    releaseMediaStream();
    if (error?.name === "NotAllowedError" || error?.name === "SecurityError") {
      showError("麦克风权限未开启。请允许当前浏览器使用麦克风后再试。", false);
    } else if (error?.name === "NotFoundError") {
      showError("没有找到可用麦克风，请检查输入设备。", false);
    } else {
      showError("无法启动麦克风，请刷新页面后重试。", false);
    }
    await endAudioFocus();
  }
}

function stopRecording() {
  if (!mediaRecorder || mediaRecorder.state === "inactive") return;
  window.clearInterval(timerInterval);
  timerInterval = null;
  setState(states.PROCESSING, "正在整理录音", "马上开始识别");
  mediaRecorder.stop();
}

async function finishRecording() {
  const durationMs = Math.round(performance.now() - recordingStartedAt);
  const mimeType = mediaRecorder?.mimeType || recordedChunks[0]?.type || "audio/webm";
  const blob = new Blob(recordedChunks, { type: mimeType });
  recordedChunks = [];
  mediaRecorder = null;
  releaseMediaStream();

  if (discardRecording) {
    discardRecording = false;
    setState(states.IDLE, "准备好了", "点击中央按钮开始录音");
    await endAudioFocus();
    return;
  }
  if (blob.size < 500) {
    showError("录音太短，没有捕获到足够声音。", true);
    await endAudioFocus();
    return;
  }
  await sendAudioTurn(blob, durationMs);
}

function releaseMediaStream() {
  if (mediaStream) {
    for (const track of mediaStream.getTracks()) track.stop();
  }
  mediaStream = null;
}

async function fetchJson(path, options) {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    Number(options?.timeoutMs) || REQUEST_TIMEOUT_MS,
  );
  try {
    const method = String(options?.method || "GET").toUpperCase();
    const headers = new Headers(options?.headers || {});
    if (requestToken) headers.set("X-Demo-Token", requestToken);
    else if (method !== "GET") throw new Error("本地安全令牌尚未就绪，请刷新页面。");
    const { timeoutMs: _timeoutMs, ...fetchOptions } = options || {};
    const response = await fetch(path, { ...fetchOptions, method, headers, signal: controller.signal });
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      throw new Error("本地服务返回了无法解析的数据。");
    }
    if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("处理时间过长，请重试。");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function sendAudioTurn(blob, durationMs) {
  setState(states.PROCESSING, "正在处理语音", "繁忙时可能需要 1–2 分钟，失败会自动重试一次");
  try {
    const result = await fetchJson("/api/turn/audio", {
      method: "POST",
      headers: {
        "Content-Type": blob.type || "audio/webm",
        "X-Session-ID": sessionId,
        "X-Audio-Duration-Ms": String(durationMs),
      },
      body: blob,
    });
    appendMessage("user", result.transcript, Boolean(result.transcript_is_placeholder));
    appendMessage("assistant", result.reply, false);
    await presentTurn(result);
  } catch (error) {
    showError(error.message || "语音处理失败，请重试。", true);
    await endAudioFocus();
  }
}

async function sendTextTurn(text) {
  audioFocusManager.beginInteraction();
  appendMessage("user", text, false);
  setState(states.PROCESSING, "正在思考", "答案生成后会自动朗读");
  try {
    const result = await fetchJson("/api/turn/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, text }),
    });
    appendMessage("assistant", result.reply, false);
    await presentTurn(result);
  } catch (error) {
    showError(error.message || "文字处理失败，请重试。", true);
    await endAudioFocus();
  }
}

async function presentTurn(result) {
  prepareClientActions(result.client_actions);
  await speakResult(result);
  await endAudioFocus();
  await loadMemories({ quiet: true });
}

function prepareClientActions(rawActions) {
  for (const action of Array.isArray(rawActions) ? rawActions : []) {
    const type = typeof action?.type === "string" ? action.type : "";
    if (type === "music.set_volume") {
      const volume = Number(action.volume);
      if (Number.isInteger(volume) && volume >= 0 && volume <= 100) {
        musicPlayer.setVolume(volume / 100);
      }
      continue;
    }
    if (type === "music.pause") {
      audioFocusManager.cancelDeferredPlayback();
      musicPlayer.pause();
      continue;
    }
    if (type === "music.stop") {
      audioFocusManager.cancelDeferredPlayback();
      musicPlayer.stop();
      continue;
    }
    if (type === "music.resume") {
      audioFocusManager.defer({ type });
      continue;
    }
    if (type === "music.play") {
      const requestedId = typeof action.station_id === "string" ? action.station_id : "";
      const stationId = requestedId || elements.stationSelect.value;
      if (!musicPlayer.hasStation(stationId)) {
        showMusicError("模型请求的电台不在本地白名单中，已拒绝播放。");
        continue;
      }
      elements.stationSelect.value = stationId;
      audioFocusManager.defer({ type, stationId });
    }
  }
}

async function endAudioFocus() {
  try {
    await audioFocusManager.endInteraction();
  } catch (error) {
    showMusicError(error.message || "无法恢复电台播放。");
  } finally {
    updateMusicUi();
  }
}

function appendMessage(role, text, placeholder) {
  const article = document.createElement("article");
  article.className = `message ${role}-message${placeholder ? " is-placeholder" : ""}`;

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "assistant" ? "声" : "你";

  const body = document.createElement("div");
  const label = document.createElement("span");
  label.className = "message-role";
  label.textContent = role === "assistant" ? "助手" : "你";
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  body.append(label, paragraph);
  article.append(avatar, body);
  elements.messages.append(article);
  elements.messages.scrollTo({ top: elements.messages.scrollHeight, behavior: "smooth" });
}

async function speakResult(result) {
  const localSpeech = result.provider_id === "siliconflow" || !result.audio_base64;
  setState(
    states.SPEAKING,
    "正在回答",
    result.tts_warning || (localSpeech ? "正在用这台 Mac 的语音朗读" : "你可以随时停止播放"),
  );
  if (result.audio_base64) {
    try {
      await playApiAudio(result.audio_base64, result.audio_mime || "audio/mpeg");
      return;
    } catch {
      // A browser may block or fail to decode MP3; system speech is a safe local fallback.
    }
  }
  await speakWithBrowser(result.reply);
}

function base64ToBlob(value, mime) {
  const raw = atob(value);
  const bytes = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index += 1) {
    bytes[index] = raw.charCodeAt(index);
  }
  return new Blob([bytes], { type: mime });
}

function playApiAudio(base64, mime) {
  return new Promise((resolve, reject) => {
    stopResponsePlayback(false);
    const blob = base64ToBlob(base64, mime);
    responseAudioUrl = URL.createObjectURL(blob);
    responseAudio = new Audio(responseAudioUrl);
    let settled = false;
    const settle = (error) => {
      if (settled) return;
      settled = true;
      responseAudioCancel = null;
      cleanupResponseAudio();
      if (error) reject(error);
      else resolve();
    };
    responseAudioCancel = () => settle();
    responseAudio.addEventListener(
      "ended",
      () => {
        finishSpeaking();
        settle();
      },
      { once: true },
    );
    responseAudio.addEventListener(
      "error",
      () => {
        settle(new Error("浏览器无法播放合成语音。"));
      },
      { once: true },
    );
    responseAudio.play().catch((error) => {
      settle(error);
    });
  });
}

function speakWithBrowser(text) {
  return new Promise((resolve) => {
    if (!("speechSynthesis" in window)) {
      finishSpeaking("浏览器不支持朗读，文字回复仍可查看。", true);
      resolve();
      return;
    }
    speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      activeSpeechCancel = null;
      resolve();
    };
    activeSpeechCancel = settle;
    const isChinese = /[\u3400-\u9fff]/.test(text);
    utterance.lang = isChinese ? "zh-CN" : "en-US";
    utterance.rate = 1.02;
    utterance.pitch = 1;
    const voices = speechSynthesis.getVoices();
    const preferred = voices.find((voice) => voice.lang.toLowerCase().startsWith(isChinese ? "zh" : "en"));
    if (preferred) utterance.voice = preferred;
    utterance.addEventListener(
      "end",
      () => {
        finishSpeaking();
        settle();
      },
      { once: true },
    );
    utterance.addEventListener(
      "error",
      () => {
        finishSpeaking("系统朗读被中断，文字回复仍可查看。", true);
        settle();
      },
      { once: true },
    );
    speechSynthesis.speak(utterance);
  });
}

function cleanupResponseAudio() {
  if (responseAudio) {
    responseAudio.pause();
    responseAudio.src = "";
  }
  if (responseAudioUrl) URL.revokeObjectURL(responseAudioUrl);
  responseAudio = null;
  responseAudioUrl = null;
}

function stopResponsePlayback(updateState = true) {
  if (responseAudioCancel) responseAudioCancel();
  else cleanupResponseAudio();
  if (activeSpeechCancel) activeSpeechCancel();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  if (updateState && currentState === states.SPEAKING) {
    setState(states.IDLE, "已停止播放", "可以继续提问");
  }
}

function finishSpeaking(detail = "点击中央按钮继续对话", keepDetail = false) {
  cleanupResponseAudio();
  setState(states.IDLE, keepDetail ? "朗读已结束" : "回答完成", detail);
}

function showError(message, appendToConversation) {
  stopResponsePlayback(false);
  if (appendToConversation) appendMessage("assistant", `出现问题：${message}`, false);
  setState(states.IDLE, "需要处理一下", message);
}

function updateMusicUi(status = musicPlayer.getStatus()) {
  const hasStations = musicPlayer.stations.size > 0;
  const locked = currentState !== states.IDLE || audioFocusManager.interactionActive;
  const stationName = status.station?.name || "";
  const stateLabels = {
    unavailable: "暂无可用电台",
    stopped: "尚未播放",
    loading: stationName ? `正在连接 · ${stationName}` : "正在连接",
    playing: stationName ? `正在播放 · ${stationName}` : "正在播放",
    paused: stationName ? `已暂停 · ${stationName}` : "已暂停",
    error: "播放遇到问题",
  };
  elements.musicState.textContent = audioFocusManager.interactionActive && status.state === "paused"
    ? "对话期间暂时暂停"
    : stateLabels[status.state] || "尚未播放";
  elements.musicState.classList.toggle("is-playing", status.state === "playing");
  elements.musicState.classList.toggle("is-error", status.state === "error");
  elements.stationSelect.disabled = !hasStations || locked;
  elements.musicPlayButton.disabled = !hasStations || locked;
  elements.musicPlayButton.textContent = status.state === "paused" ? "继续" : "播放";
  elements.musicPauseButton.disabled = locked || status.state !== "playing";
  elements.musicStopButton.disabled = locked || !status.stationId;
  elements.musicVolume.disabled = !hasStations;
  elements.musicVolume.value = String(Math.round(status.volume * 100));
  elements.musicVolumeValue.value = `${Math.round(status.volume * 100)}%`;
}

function showMusicError(message) {
  elements.musicState.textContent = message;
  elements.musicState.classList.remove("is-playing");
  elements.musicState.classList.add("is-error");
}

async function loadStations() {
  elements.musicState.textContent = "正在载入电台";
  try {
    const payload = await fetchJson("/api/music/stations", {
      method: "GET",
      timeoutMs: MUSIC_REQUEST_TIMEOUT_MS,
    });
    const stations = Array.isArray(payload) ? payload : payload.stations;
    musicPlayer.loadStations(stations);
    const previousSelection = elements.stationSelect.value;
    elements.stationSelect.replaceChildren();
    for (const station of musicPlayer.stations.values()) {
      const option = document.createElement("option");
      option.value = station.id;
      option.textContent = station.description ? `${station.name} · ${station.description}` : station.name;
      elements.stationSelect.append(option);
    }
    if (musicPlayer.hasStation(previousSelection)) elements.stationSelect.value = previousSelection;
    if (!musicPlayer.stations.size) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "暂无可用电台";
      elements.stationSelect.append(option);
    }
    updateMusicUi();
  } catch (error) {
    musicPlayer.loadStations([]);
    elements.stationSelect.replaceChildren();
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "电台功能尚未就绪";
    elements.stationSelect.append(option);
    showMusicError(error.message || "无法载入电台列表。");
  }
}

function memoryText(memory) {
  for (const candidate of [memory?.summary, memory?.text, memory?.value]) {
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return "未命名记忆";
}

function memoryKindLabel(kind) {
  const labels = {
    preference: "偏好",
    profile: "资料",
    person: "人物",
    place: "地点",
    project: "项目",
    other: "其他",
  };
  return labels[kind] || "记忆";
}

function renderMemories(memories) {
  elements.memoryList.replaceChildren();
  const validMemories = Array.isArray(memories)
    ? memories.filter((memory) => memory && (typeof memory.id === "string" || typeof memory.id === "number"))
    : [];
  elements.clearMemoriesButton.disabled = !validMemories.length;
  if (!validMemories.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "还没有长期记忆。可以说“记住我喜欢爵士乐”。";
    elements.memoryList.append(empty);
    return;
  }

  for (const memory of validMemories) {
    const row = document.createElement("article");
    row.className = "memory-item";
    const body = document.createElement("div");
    const meta = document.createElement("span");
    meta.className = "memory-meta";
    meta.textContent = memory.sensitive
      ? `${memoryKindLabel(memory.kind)} · 仅本机私密`
      : memoryKindLabel(memory.kind);
    const content = document.createElement("p");
    content.textContent = memoryText(memory);
    body.append(meta, content);

    const deleteButton = document.createElement("button");
    deleteButton.className = "memory-delete";
    deleteButton.type = "button";
    deleteButton.textContent = "忘记";
    deleteButton.setAttribute("aria-label", `忘记：${memoryText(memory)}`);
    deleteButton.addEventListener("click", async () => {
      deleteButton.disabled = true;
      try {
        await fetchJson(`/api/memories/${encodeURIComponent(String(memory.id))}`, {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
        });
        await loadMemories();
      } catch (error) {
        deleteButton.disabled = false;
        showMemoryError(error.message || "无法删除这条记忆。");
      }
    });
    row.append(body, deleteButton);
    elements.memoryList.append(row);
  }
}

function showMemoryError(message) {
  elements.memoryList.replaceChildren();
  const error = document.createElement("p");
  error.className = "empty-state is-error";
  error.textContent = message;
  elements.memoryList.append(error);
}

async function loadMemories({ quiet = false } = {}) {
  elements.refreshMemoriesButton.disabled = true;
  if (!quiet) {
    elements.memoryList.replaceChildren();
    const loading = document.createElement("p");
    loading.className = "empty-state";
    loading.textContent = "正在读取本地记忆…";
    elements.memoryList.append(loading);
  }
  try {
    const payload = await fetchJson("/api/memories", {
      method: "GET",
      timeoutMs: MUSIC_REQUEST_TIMEOUT_MS,
    });
    renderMemories(Array.isArray(payload) ? payload : (payload.memories || payload.items));
  } catch (error) {
    if (!quiet) showMemoryError(error.message || "无法读取长期记忆。");
  } finally {
    elements.refreshMemoriesButton.disabled = false;
  }
}

async function clearMemories() {
  if (!window.confirm("确定要让音箱忘记全部长期记忆吗？这个操作无法撤销。")) return;
  elements.clearMemoriesButton.disabled = true;
  try {
    await fetchJson("/api/memories/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    await loadMemories();
  } catch (error) {
    showMemoryError(error.message || "无法清空长期记忆。");
  }
}

async function loadHealth() {
  try {
    const health = await fetchJson("/api/health", { method: "GET" });
    if (!health.request_token) throw new Error("本地服务没有提供安全令牌。");
    requestToken = health.request_token;
    currentProviderId = health.provider_id || "mock";
    elements.modeBadge.className = `mode-badge ${health.real_api_enabled ? "is-real" : "is-mock"}`;
    const providerName = currentProviderId === "siliconflow" ? "SiliconFlow" : health.provider_label;
    elements.modeLabel.textContent = health.real_api_enabled
      ? `真实 API · ${providerName}`
      : "本地演示 · 不上传录音";
    if (health.real_api_enabled) {
      elements.privacyCard.classList.add("is-real");
      elements.privacyTitle.textContent = currentProviderId === "siliconflow"
        ? "SiliconFlow 云端模式已开启"
        : `${health.provider_label} 云端模式已开启`;
    } else {
      elements.privacyCard.classList.remove("is-real");
      elements.privacyTitle.textContent = "本地演示保护已开启";
    }
    elements.privacyText.textContent = health.privacy_notice;
    elements.disclosure.textContent = health.ai_voice_disclosure;
    setState(states.IDLE, "准备好了", "点击中央按钮开始录音");
    await Promise.allSettled([loadStations(), loadMemories()]);
  } catch (error) {
    elements.modeBadge.className = "mode-badge is-error";
    elements.modeLabel.textContent = "本地服务连接异常";
    setState(states.STARTING, "本地服务连接失败", error.message || "请刷新页面后重试。");
  }
}

async function resetConversation() {
  if (currentState === states.RECORDING) {
    discardRecording = true;
    stopRecording();
  }
  stopResponsePlayback(false);
  await endAudioFocus();
  elements.messages.replaceChildren();
  appendMessage("assistant", "对话已经清空。准备好后，可以重新开始。", false);
  try {
    await fetchJson("/api/session/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });
    sessionId = createSessionId();
    setState(states.IDLE, "对话已清空", "点击中央按钮重新开始");
  } catch (error) {
    showError(error.message || "无法清空对话。", false);
  }
}

function resizeTextArea() {
  elements.textInput.style.height = "auto";
  elements.textInput.style.height = `${Math.min(elements.textInput.scrollHeight, 110)}px`;
}

elements.micButton.addEventListener("click", () => {
  if (currentState === states.IDLE) startRecording();
  else if (currentState === states.RECORDING) stopRecording();
});

elements.stopAudioButton.addEventListener("click", () => stopResponsePlayback(true));
elements.resetButton.addEventListener("click", resetConversation);
elements.refreshMemoriesButton.addEventListener("click", () => loadMemories());
elements.clearMemoriesButton.addEventListener("click", clearMemories);

elements.musicPlayButton.addEventListener("click", async () => {
  const stationId = elements.stationSelect.value;
  if (!musicPlayer.hasStation(stationId)) {
    showMusicError("请选择一个白名单电台。");
    return;
  }
  try {
    await musicPlayer.play(stationId);
  } catch (error) {
    showMusicError(error.message || "无法播放这个电台。");
  }
});

elements.musicPauseButton.addEventListener("click", () => {
  audioFocusManager.cancelAutoResume();
  musicPlayer.pause();
});

elements.musicStopButton.addEventListener("click", () => {
  audioFocusManager.cancelAutoResume();
  musicPlayer.stop();
});

elements.musicVolume.addEventListener("input", () => {
  musicPlayer.setVolume(Number(elements.musicVolume.value) / 100);
});

elements.textForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (currentState !== states.IDLE) return;
  const text = elements.textInput.value.trim();
  if (!text) return;
  elements.textInput.value = "";
  resizeTextArea();
  sendTextTurn(text);
});

elements.textInput.addEventListener("input", resizeTextArea);
elements.textInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    elements.textForm.requestSubmit();
  }
});

window.addEventListener("beforeunload", () => {
  releaseMediaStream();
  stopResponsePlayback(false);
  musicPlayer.stop();
});

setState(states.STARTING, "正在连接本地服务", "准备安全会话");
loadHealth();
