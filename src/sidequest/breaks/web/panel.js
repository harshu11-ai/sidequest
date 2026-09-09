"use strict";

// Shared across off.html (the full toggle panel), chess.html and video.html
// (the pinned mini-strip on each) -- every toggle control on any of these
// pages is a checkbox carrying `data-toggle="chess"|"video"`, so this one
// file can wire all three without knowing which page it's on. Elements that
// only exist on off.html (settings fields, the mode/footer labels) are
// looked up defensively and simply skipped where they're absent.

// Named breaksToken, not token -- this script loads alongside chess/app.js
// or video/app.js on the same page, as a plain (non-module) <script>, so it
// shares their top-level scope; both already declare their own `const
// token`, and a second `const token` at that scope is a SyntaxError that
// silently kills this whole script (caught live in a real-browser smoke
// test, not by the Python test suite, which never executes the JS).
const breaksToken = new URLSearchParams(window.location.search).get("token") || "";

async function fetchBreaksState() {
  const response = await fetch(`/api/breaks/state?token=${encodeURIComponent(breaksToken)}`, {
    cache: "no-store",
  });
  if (!response.ok) throw new Error("Unable to read breaks state");
  return response.json();
}

async function postBreaks(path, payload) {
  const response = await fetch(`${path}?token=${encodeURIComponent(breaksToken)}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function applyState(state) {
  for (const input of document.querySelectorAll("[data-toggle]")) {
    input.checked = Boolean(state.toggles[input.dataset.toggle]);
  }

  const modeLabel = document.querySelector("#breaks-mode-label");
  if (modeLabel) {
    modeLabel.textContent =
      state.toggles.chess && state.toggles.video ? "RANDOM"
        : state.toggles.chess ? "CHESS"
          : state.toggles.video ? "VIDEO"
            : "OFF";
  }

  const randomNote = document.querySelector("#random-note");
  if (randomNote) randomNote.hidden = !(state.toggles.chess && state.toggles.video);

  // "All set" only makes sense once there's something to confirm -- with
  // nothing toggled on, the panel has no close affordance at all and just
  // sits open until the user dismisses it themselves.
  const confirmButton = document.querySelector("#breaks-confirm-button");
  if (confirmButton) confirmButton.hidden = !(state.toggles.chess || state.toggles.video);

  const footerNote = document.querySelector("#footer-note");
  if (footerNote) {
    footerNote.textContent =
      state.toggles.chess && state.toggles.video ? "Could be chess, could be a video. You'll see."
        : state.toggles.chess ? "Chess is on for your next prompt."
          : state.toggles.video ? "Video is on for your next prompt."
            : "Nothing's on — your agent runs straight through.";
  }

  const difficulty = document.querySelector("#breaks-difficulty");
  if (difficulty && document.activeElement !== difficulty) {
    difficulty.value = state.settings.difficulty || "medium";
  }

  return state;
}

// Named refreshBreaksState, not refresh -- chess/app.js declares its own
// top-level `refresh` on chess.html. Unlike the `const token` collision
// above, two `function refresh(){}` declarations don't throw; the second
// one silently shadows the first in the shared global scope instead, which
// only "worked" here by luck of the two scripts' own setInterval calls each
// capturing their own function reference before the other one loaded. Not
// worth relying on -- keep the name unique instead.
async function refreshBreaksState() {
  try {
    applyState(await fetchBreaksState());
  } catch (error) {
    // Best-effort: leave the panel showing its last known state.
  }
}

function wireToggle(input) {
  input.addEventListener("change", async () => {
    const message = document.querySelector("#breaks-message");
    try {
      applyState(await postBreaks("/api/breaks/toggle", {mode: input.dataset.toggle, on: input.checked}));
    } catch (error) {
      input.checked = !input.checked;
      if (message) message.textContent = error.message;
    }
  });
}

function wireExpand(buttonId, blockId) {
  const button = document.querySelector(`#${buttonId}`);
  const block = document.querySelector(`#${blockId}`);
  if (!button || !block) return;
  button.addEventListener("click", () => {
    const expanded = button.getAttribute("aria-expanded") === "true";
    button.setAttribute("aria-expanded", String(!expanded));
    block.hidden = expanded;
  });
}

async function saveSettings(partial) {
  const message = document.querySelector("#breaks-message");
  try {
    applyState(await postBreaks("/api/breaks/settings", partial));
    if (message) message.textContent = "";
  } catch (error) {
    if (message) message.textContent = error.message;
  }
}

document.querySelectorAll("[data-toggle]").forEach(wireToggle);
wireExpand("chess-expand", "chess-settings");
wireExpand("video-expand", "video-settings");

const difficultySelect = document.querySelector("#breaks-difficulty");
if (difficultySelect) {
  difficultySelect.addEventListener("change", () => saveSettings({difficulty: difficultySelect.value}));
}

// -- Chess: Solo/Multiplayer segmented control + Host/Join (off.html only) --

const soloFields = document.querySelector("#chess-solo-fields");
const multiplayerFields = document.querySelector("#chess-multiplayer-fields");
const chessModeButtons = document.querySelectorAll("[data-chess-mode]");
const hostJoinTabs = document.querySelectorAll("[data-host-join-tab]");
const joinCodeRow = document.querySelector("#multiplayer-join-code-row");
const hostButton = document.querySelector("#multiplayer-host-button");
const joinButton = document.querySelector("#multiplayer-join-button");
// Named multiplayerRoomCodeLabel, not roomCodeLabel -- chess/app.js
// declares its own top-level `roomCodeLabel` on chess.html, and a second
// `const roomCodeLabel` at that shared scope is the same SyntaxError class
// the breaksToken/refreshBreaksState renames above already worked around.
const multiplayerRoomCodeLabel = document.querySelector("#multiplayer-room-code");

function selectChessMode(mode) {
  for (const button of chessModeButtons) {
    button.classList.toggle("segment--active", button.dataset.chessMode === mode);
  }
  if (soloFields) soloFields.hidden = mode !== "solo";
  if (multiplayerFields) multiplayerFields.hidden = mode !== "multiplayer";
}

function selectHostJoinTab(tab) {
  for (const button of hostJoinTabs) {
    button.classList.toggle("subtab--active", button.dataset.hostJoinTab === tab);
  }
  if (joinCodeRow) joinCodeRow.hidden = tab !== "join";
  if (hostButton) hostButton.hidden = tab !== "host";
  if (joinButton) joinButton.hidden = tab !== "join";
}

for (const button of chessModeButtons) {
  button.addEventListener("click", () => selectChessMode(button.dataset.chessMode));
}
for (const button of hostJoinTabs) {
  button.addEventListener("click", () => selectHostJoinTab(button.dataset.hostJoinTab));
}
if (chessModeButtons.length) selectChessMode("solo");
if (hostJoinTabs.length) selectHostJoinTab("host");

async function startMultiplayer(path, extra) {
  const message = document.querySelector("#breaks-message");
  try {
    const result = await postBreaks(path, extra);
    if (multiplayerRoomCodeLabel && result.multiplayer) multiplayerRoomCodeLabel.textContent = result.multiplayer.room_code;
    if (message) message.textContent = "";
    // The host/join endpoints return chess state, not breaks state (they
    // auto-toggle chess on server-side) -- re-fetch to pick that up.
    await refreshBreaksState();
  } catch (error) {
    if (message) message.textContent = error.message;
  }
}

if (hostButton) {
  hostButton.addEventListener("click", () => startMultiplayer("/api/multiplayer/host", {}));
}
if (joinButton) {
  joinButton.addEventListener("click", () => {
    const codeInput = document.querySelector("#multiplayer-code-input");
    startMultiplayer("/api/multiplayer/join", {code: codeInput ? codeInput.value : ""});
  });
}

const confirmButton = document.querySelector("#breaks-confirm-button");
if (confirmButton) {
  confirmButton.addEventListener("click", async () => {
    try {
      await postBreaks("/api/breaks/confirm", {});
    } finally {
      window.close();
    }
  });
}

refreshBreaksState();
window.setInterval(refreshBreaksState, 1500);
