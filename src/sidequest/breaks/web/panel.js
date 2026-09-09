"use strict";

// Shared across off.html (the full toggle panel), chess.html and video.html
// (the pinned mini-strip on each) -- every toggle control on any of these
// pages is a checkbox carrying `data-toggle="chess"|"video"`, so this one
// file can wire all three without knowing which page it's on. Elements that
// only exist on off.html (settings fields, the mode/footer labels) are
// looked up defensively and simply skipped where they're absent.

const token = new URLSearchParams(window.location.search).get("token") || "";

async function fetchBreaksState() {
  const response = await fetch(`/api/breaks/state?token=${encodeURIComponent(token)}`, {
    cache: "no-store",
  });
  if (!response.ok) throw new Error("Unable to read breaks state");
  return response.json();
}

async function postBreaks(path, payload) {
  const response = await fetch(`${path}?token=${encodeURIComponent(token)}`, {
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
  const stockfishPath = document.querySelector("#breaks-stockfish-path");
  if (stockfishPath && document.activeElement !== stockfishPath) {
    stockfishPath.value = state.settings.stockfish_path || "";
  }

  return state;
}

async function refresh() {
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
const stockfishInput = document.querySelector("#breaks-stockfish-path");
if (stockfishInput) {
  stockfishInput.addEventListener("change", () => saveSettings({stockfish_path: stockfishInput.value}));
}

refresh();
window.setInterval(refresh, 1500);
