"use strict";

const token = new URLSearchParams(window.location.search).get("token") || "";
const titleLabel = document.querySelector("#video-title");
const channelLabel = document.querySelector("#video-channel");
const playIcon = document.querySelector("#play-icon");
const pauseIcon = document.querySelector("#pause-icon");
const elapsedLabel = document.querySelector("#elapsed");
const durationLabel = document.querySelector("#duration");
const progressFill = document.querySelector("#progress-fill");
const queueList = document.querySelector("#queue-list");
const finished = document.querySelector("#finished");
const lastTurnNotice = document.querySelector("#last-turn-notice");
const agentStatusLabel = document.querySelector("#agent-status");
const agentDot = document.querySelector("#agent-dot");

let player = null;
let ytApiReady = false;
let state = null;
let catalog = null;
let wasActive = false;
let closing = false;

function formatTime(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds || 0));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

async function request(path, payload) {
  const response = await fetch(`${path}?token=${encodeURIComponent(token)}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload || {}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function fetchState() {
  const response = await fetch(`/api/state?token=${encodeURIComponent(token)}`, {cache: "no-store"});
  if (!response.ok) throw new Error("Unable to read queue state");
  return response.json();
}

async function fetchCatalog() {
  const response = await fetch(`/api/catalog?token=${encodeURIComponent(token)}`, {cache: "no-store"});
  if (!response.ok) throw new Error("Unable to read catalog");
  return response.json();
}

function renderQueue() {
  if (!state || !catalog) return;
  queueList.innerHTML = "";
  const upcoming = Math.min(5, catalog.length);
  for (let offset = 0; offset < upcoming; offset += 1) {
    const position = (state.index + offset) % catalog.length;
    const entry = catalog[position];
    const item = document.createElement("li");
    item.className = "queue-item";
    if (offset === 0) item.classList.add("queue-item--current");
    if (state.watched.includes(entry.id)) item.classList.add("queue-item--watched");
    item.textContent = `${entry.title} — ${entry.channel}`;
    queueList.appendChild(item);
  }
}

function renderVideoInfo() {
  titleLabel.textContent = state.video.title;
  channelLabel.textContent = state.video.channel;
  durationLabel.textContent = formatTime(state.video.duration_s);
}

function renderAgentStatus(active) {
  agentStatusLabel.textContent = active ? "Agent working" : "Agent ready";
  agentDot.classList.toggle("status-dot--online", active);
}

function setPlayingIcon(isPlaying) {
  playIcon.hidden = isPlaying;
  pauseIcon.hidden = !isPlaying;
}

function maybeCreatePlayer() {
  if (!ytApiReady || !state || player) return;
  player = new YT.Player("yt-player", {
    host: "https://www.youtube-nocookie.com",
    videoId: state.video.youtube_id,
    playerVars: {controls: 0, modestbranding: 1, rel: 0, iv_load_policy: 3},
    events: {onReady: onPlayerReady, onStateChange: onPlayerStateChange},
  });
}

function onPlayerReady() {
  if (state.position_s > 0) player.seekTo(state.position_s, true);
  if (state.active) player.playVideo();
}

function onPlayerStateChange(event) {
  if (event.data === YT.PlayerState.PLAYING) setPlayingIcon(true);
  else if (event.data === YT.PlayerState.PAUSED) setPlayingIcon(false);
  else if (event.data === YT.PlayerState.ENDED) advance("skip");
}

async function advance(action) {
  try {
    const nextState = await request(action === "skip" ? "/api/skip" : "/api/previous", {});
    state = nextState;
    renderVideoInfo();
    renderQueue();
    if (player) player.loadVideoById(state.video.youtube_id, state.position_s || 0);
  } catch (error) {
    // Best-effort: leave the current video playing rather than breaking the
    // window over a queue-advance hiccup.
  }
}

function savePosition() {
  if (!player || typeof player.getCurrentTime !== "function") return;
  request("/api/position", {position_s: player.getCurrentTime()}).catch(() => {});
}

function updateProgress() {
  if (!player || typeof player.getCurrentTime !== "function") return;
  const elapsed = player.getCurrentTime();
  const total = player.getDuration() || (state?.video?.duration_s ?? 0);
  elapsedLabel.textContent = formatTime(elapsed);
  if (total > 0) {
    durationLabel.textContent = formatTime(total);
    progressFill.style.width = `${Math.min(100, (elapsed / total) * 100)}%`;
  }
}

async function pollActive() {
  try {
    const nextState = await fetchState();
    renderAgentStatus(nextState.active);
    if (nextState.active) {
      wasActive = true;
      lastTurnNotice.hidden = true;
      finished.hidden = true;
    } else if (wasActive && !closing) {
      closing = true;
      savePosition();
      if (player) player.pauseVideo();
      finished.hidden = false;
      window.setTimeout(() => window.close(), 350);
    }
  } catch (error) {
    // Transient network hiccups shouldn't spam the UI; the next poll retries.
  }
}

function togglePlayPause() {
  if (!player) return;
  if (player.getPlayerState() === YT.PlayerState.PLAYING) player.pauseVideo();
  else player.playVideo();
}

async function closeVideoWindow() {
  savePosition();
  try {
    await request("/lifecycle/stop", {});
  } finally {
    window.close();
  }
}

window.onYouTubeIframeAPIReady = function onYouTubeIframeAPIReady() {
  ytApiReady = true;
  maybeCreatePlayer();
};

document.querySelector("#play-pause").addEventListener("click", togglePlayPause);
document.querySelector("#next").addEventListener("click", () => advance("skip"));
document.querySelector("#prev").addEventListener("click", () => advance("previous"));
document.querySelector("#return").addEventListener("click", closeVideoWindow);
document.querySelector("#close-window").addEventListener("click", () => window.close());

(async function init() {
  try {
    [state, catalog] = await Promise.all([fetchState(), fetchCatalog()]);
    renderVideoInfo();
    renderAgentStatus(state.active);
    renderQueue();
    maybeCreatePlayer();
  } catch (error) {
    titleLabel.textContent = "Unable to load the video queue.";
  }
})();

window.setInterval(pollActive, 500);
window.setInterval(savePosition, 5000);
window.setInterval(updateProgress, 500);
