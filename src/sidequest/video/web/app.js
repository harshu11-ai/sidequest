"use strict";

const token = new URLSearchParams(window.location.search).get("token") || "";
const titleLabel = document.querySelector("#video-title");
const channelLabel = document.querySelector("#video-channel");
const queueList = document.querySelector("#queue-list");
const finished = document.querySelector("#finished");
const lastTurnNotice = document.querySelector("#last-turn-notice");
const agentStatusLabel = document.querySelector("#agent-status");
const agentDot = document.querySelector("#agent-dot");
const prevButton = document.querySelector("#prev");

let player = null;
let ytApiReady = false;
let state = null;
let wasActive = false;
let closing = false;

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
  const response = await fetch(`/api/video/state?token=${encodeURIComponent(token)}`, {cache: "no-store"});
  if (!response.ok) throw new Error("Unable to read queue state");
  return response.json();
}

function renderQueue() {
  queueList.innerHTML = "";
  const current = document.createElement("li");
  current.className = "queue-item queue-item--current";
  current.textContent = `${state.video.title} — ${state.video.channel}`;
  queueList.appendChild(current);
  for (const entry of state.upcoming) {
    const item = document.createElement("li");
    item.className = "queue-item";
    if (state.watched.includes(entry.id)) item.classList.add("queue-item--watched");
    item.textContent = `${entry.title} — ${entry.channel}`;
    queueList.appendChild(item);
  }
}

function renderVideoInfo() {
  titleLabel.textContent = state.video.title;
  channelLabel.textContent = state.video.channel;
  prevButton.disabled = !state.can_go_back;
}

function renderAgentStatus(active) {
  agentStatusLabel.textContent = active ? "Agent working" : "Agent ready";
  agentDot.classList.toggle("status-dot--online", active);
}

function maybeCreatePlayer() {
  if (!ytApiReady || !state || player) return;
  player = new YT.Player("yt-player", {
    host: "https://www.youtube-nocookie.com",
    videoId: state.video.youtube_id,
    // Deliberately YouTube's own control bar (seek, play/pause, captions,
    // fullscreen, ...) -- sidequest only adds a "next video" button outside
    // the player, rather than rebuilding transport controls YouTube already
    // gives us for free.
    playerVars: {rel: 0},
    events: {onReady: onPlayerReady, onStateChange: onPlayerStateChange},
  });
}

function onPlayerReady() {
  if (state.position_s > 0) player.seekTo(state.position_s, true);
  if (state.active) player.playVideo();
}

function onPlayerStateChange(event) {
  if (event.data === YT.PlayerState.ENDED) nextVideo();
}

async function nextVideo() {
  await advance("/api/video/skip");
}

async function previousVideo() {
  if (!state.can_go_back) return;
  await advance("/api/video/previous");
}

async function advance(path) {
  try {
    state = await request(path, {});
    renderVideoInfo();
    renderQueue();
    if (player) player.loadVideoById(state.video.youtube_id, 0);
  } catch (error) {
    // Best-effort: leave the current video playing rather than breaking the
    // window over a queue-advance hiccup.
  }
}

function savePosition() {
  if (!player || typeof player.getCurrentTime !== "function") return;
  request("/api/video/position", {position_s: player.getCurrentTime()}).catch(() => {});
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
      window.setTimeout(collapseWindow, 350);
    }
  } catch (error) {
    // Transient network hiccups shouldn't spam the UI; the next poll retries.
  }
}

async function collapseWindow() {
  // The window persists across turns (see BreaksCompanion) -- this asks the
  // server to shrink it back to the toggle panel in place, rather than
  // closing it the way earlier versions did.
  try {
    await fetch(`/api/breaks/collapse?token=${encodeURIComponent(token)}`, {method: "POST"});
  } catch (error) {
    // Best-effort -- the next turn's navigate() puts the window back in the
    // right place regardless.
  }
}

async function closeVideoWindow() {
  savePosition();
  try {
    await request("/lifecycle/stop", {});
  } finally {
    collapseWindow();
  }
}

window.onYouTubeIframeAPIReady = function onYouTubeIframeAPIReady() {
  ytApiReady = true;
  maybeCreatePlayer();
};

document.querySelector("#next").addEventListener("click", nextVideo);
prevButton.addEventListener("click", previousVideo);
document.querySelector("#return").addEventListener("click", closeVideoWindow);
document.querySelector("#close-window").addEventListener("click", collapseWindow);

(async function init() {
  try {
    state = await fetchState();
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
