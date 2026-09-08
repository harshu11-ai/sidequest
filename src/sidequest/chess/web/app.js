"use strict";

const token = new URLSearchParams(window.location.search).get("token") || "";
const boardElement = document.querySelector("#board");
const statusLabel = document.querySelector("#status");
const engineLabel = document.querySelector("#engine");
const difficulty = document.querySelector("#difficulty");
const message = document.querySelector("#message");
const finished = document.querySelector("#finished");
const lastTurnNotice = document.querySelector("#last-turn-notice");
const sideNote = document.querySelector("#side-note");
const newGameButton = document.querySelector("#new-game");
const practicePanel = document.querySelector("#practice-panel");
const multiplayerPanel = document.querySelector("#multiplayer-panel");
const roomCodeLabel = document.querySelector("#room-code");
const opponentDot = document.querySelector("#opponent-dot");
const opponentStatusLabel = document.querySelector("#opponent-status");
const togglePracticeButton = document.querySelector("#toggle-practice");
const lastMoveMarker = {class: "marker-square-last", slice: "markerSquare"};
const animationDuration = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 1 : 260;
let chessboard = null;
let state = null;
let pendingMove = null;
let wasActive = false;
let isAnimating = false;
let awaitingLastTurn = false;

function positionPart(fen) {
  return fen.split(" ")[0];
}

function yourColor() {
  if (state?.mode === "multiplayer" && state.multiplayer) return state.multiplayer.you;
  return "white";
}

function legalMovesFrom(square) {
  if (!state || state.turn !== yourColor()) return [];
  return state.legal_moves.filter((move) => move.startsWith(square));
}

function inputHandler(event) {
  if (event.type === CMChessboard.INPUT_EVENT_TYPE.movingOverSquare) return;
  if (event.type !== CMChessboard.INPUT_EVENT_TYPE.moveInputFinished) {
    event.chessboard.removeLegalMovesMarkers();
  }

  if (event.type === CMChessboard.INPUT_EVENT_TYPE.moveInputStarted) {
    if (isAnimating) return false;
    const moves = legalMovesFrom(event.squareFrom);
    event.chessboard.addLegalMovesMarkers(
      moves.map((move) => ({to: move.slice(2, 4), promotion: move[4]})),
    );
    return moves.length > 0;
  }

  if (event.type === CMChessboard.INPUT_EVENT_TYPE.validateMoveInput) {
    const candidates = legalMovesFrom(event.squareFrom).filter(
      (move) => move.slice(2, 4) === event.squareTo,
    );
    pendingMove = candidates.find((move) => move.endsWith("q")) || candidates[0] || null;
    return pendingMove !== null;
  }

  if (event.type === CMChessboard.INPUT_EVENT_TYPE.moveInputFinished && event.legalMove) {
    event.chessboard.disableMoveInput();
    const move = pendingMove;
    pendingMove = null;
    event.chessboard.state.moveInputProcess.then(() => submitMove(move));
  }
}

let enabledInputColor = null;

function enableInputIfReady() {
  if (!chessboard) return;
  const desiredColor = yourColor();
  const shouldEnable =
    (state?.active || awaitingLastTurn) && state.turn === desiredColor && !isAnimating;
  if (shouldEnable) {
    // Re-register whenever the color changes too, not just when input was off --
    // switching between multiplayer (as Black, say) and practice (always White)
    // otherwise leaves the board listening for the wrong color's pieces.
    if (!chessboard.isMoveInputEnabled() || enabledInputColor !== desiredColor) {
      chessboard.enableMoveInput(inputHandler, CMChessboard.COLOR[desiredColor]);
      enabledInputColor = desiredColor;
    }
  } else if (chessboard.isMoveInputEnabled()) {
    chessboard.disableMoveInput();
    enabledInputColor = null;
  }
}

function showLastMove() {
  if (!chessboard?.removeMarkers) return;
  chessboard.removeMarkers(lastMoveMarker);
  if (!state?.last_move) return;
  chessboard.addMarker(lastMoveMarker, state.last_move.slice(0, 2));
  chessboard.addMarker(lastMoveMarker, state.last_move.slice(2, 4));
}

function renderMetadata(nextState) {
  state = nextState;
  statusLabel.textContent = state.status;
  showLastMove();

  const multiplayer = state.multiplayer;
  const inMultiplayerMode = Boolean(multiplayer) && state.mode === "multiplayer";
  multiplayerPanel.hidden = !multiplayer;
  practicePanel.hidden = inMultiplayerMode;
  newGameButton.hidden = inMultiplayerMode;

  if (multiplayer) {
    roomCodeLabel.textContent = multiplayer.room_code;
    opponentStatusLabel.textContent = multiplayer.opponent_connected
      ? "Connected"
      : multiplayer.room_status === "waiting_for_opponent"
        ? "Waiting to join"
        : "Disconnected";
    opponentDot.classList.toggle("status-dot--online", multiplayer.opponent_connected);
    opponentDot.classList.toggle("status-dot--offline", !multiplayer.opponent_connected);
    togglePracticeButton.textContent =
      state.mode === "multiplayer" ? "Play vs bot while you wait" : "Back to multiplayer game";
    sideNote.textContent = inMultiplayerMode
      ? `You play ${multiplayer.you === "white" ? "White" : "Black"}`
      : "You play White (practice)";
  } else {
    sideNote.textContent = "You play White";
  }

  if (!inMultiplayerMode) {
    engineLabel.textContent = state.engine;
    difficulty.value = state.difficulty || "medium";
    difficulty.disabled = state.last_move != null;
    difficulty.title = difficulty.disabled
      ? "Difficulty is locked once the game has started. Start a new game to change it."
      : "";
  }

  if (state.active) {
    wasActive = true;
    awaitingLastTurn = false;
    lastTurnNotice.hidden = true;
    finished.hidden = true;
  } else if (wasActive) {
    const gameOver = state.status === "Checkmate" || state.status === "Stalemate";
    if (!awaitingLastTurn && !gameOver && state.turn === yourColor()) {
      // The agent finished, but it's genuinely this player's move -- give them
      // one more turn instead of yanking the window shut mid-thought.
      awaitingLastTurn = true;
      lastTurnNotice.hidden = false;
    } else if (!awaitingLastTurn || gameOver || state.turn !== yourColor()) {
      closeAfterFinish();
    }
  }
  enableInputIfReady();
}

function closeAfterFinish() {
  wasActive = false;
  awaitingLastTurn = false;
  lastTurnNotice.hidden = true;
  finished.hidden = false;
  window.setTimeout(() => window.close(), 350);
}

function createBoard(initialState) {
  state = initialState;
  chessboard = new CMChessboard.Chessboard(boardElement, {
    position: initialState.fen,
    orientation: CMChessboard.COLOR[yourColor()],
    assetsUrl: "/",
    style: {
      cssClass: "sidequest-board",
      showCoordinates: false,
      borderType: CMChessboard.BORDER_TYPE.none,
      animationDuration,
      pieces: {file: "/cm-standard.svg"},
    },
    extensions: [
      {
        class: CMMarkers.Markers,
        props: {
          autoMarkers: CMMarkers.MARKER_TYPE.square,
          sprite: "/cm-markers.svg",
        },
      },
    ],
  });
  renderMetadata(initialState);
}

async function submitMove(move) {
  if (!move || isAnimating) return;
  const previousState = state;
  const wasAwaitingLastTurn = awaitingLastTurn;
  isAnimating = true;
  statusLabel.textContent = "Computer thinking";
  try {
    const nextState = await request("/api/move", {move});
    if (nextState.player_fen) {
      await chessboard.setPosition(nextState.player_fen, true);
    }
    if (nextState.computer_move) {
      statusLabel.textContent = "Opponent moving";
      await chessboard.setPosition(nextState.fen, true);
    } else if (!nextState.player_fen) {
      // Multiplayer moves have neither field -- just show the result directly.
      await chessboard.setPosition(nextState.fen, true);
    }
    isAnimating = false;
    if (wasAwaitingLastTurn) {
      // They just took the one extra move we promised them -- close now,
      // regardless of whose turn it is next (practice's bot replies inline,
      // which would otherwise hand the turn straight back to them).
      renderMetadata(nextState);
      closeAfterFinish();
      return;
    }
    renderMetadata(nextState);
  } catch (error) {
    message.textContent = error.message;
    await chessboard.setPosition(previousState.fen, true);
    isAnimating = false;
    renderMetadata(previousState);
  }
}

async function request(path, payload) {
  message.textContent = "";
  const response = await fetch(`${path}?token=${encodeURIComponent(token)}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const nextState = await response.json();
  if (!response.ok) throw new Error(nextState.error || "Request failed");
  return nextState;
}

async function resetGame() {
  if (isAnimating) return;
  isAnimating = true;
  enableInputIfReady();
  try {
    const nextState = await request("/api/new", {});
    await chessboard.setPosition(nextState.fen, true);
    isAnimating = false;
    renderMetadata(nextState);
  } catch (error) {
    isAnimating = false;
    message.textContent = error.message;
    enableInputIfReady();
  }
}

async function changeDifficulty() {
  try {
    renderMetadata(await request("/api/difficulty", {difficulty: difficulty.value}));
  } catch (error) {
    message.textContent = error.message;
    difficulty.value = state.difficulty;
  }
}

async function toggleMode() {
  if (isAnimating || !state) return;
  const nextMode = state.mode === "multiplayer" ? "practice" : "multiplayer";
  isAnimating = true;
  try {
    const nextState = await request("/api/mode", {mode: nextMode});
    state = nextState;
    if (chessboard) {
      chessboard.setOrientation(CMChessboard.COLOR[yourColor()]);
      await chessboard.setPosition(nextState.fen, false);
    }
    isAnimating = false;
    renderMetadata(nextState);
  } catch (error) {
    isAnimating = false;
    message.textContent = error.message;
  }
}

async function refresh() {
  if (isAnimating) return;
  try {
    const response = await fetch(`/api/state?token=${encodeURIComponent(token)}`, {cache: "no-store"});
    if (!response.ok) throw new Error("Unable to read game state");
    const nextState = await response.json();
    if (!chessboard) {
      createBoard(nextState);
      return;
    }
    if (positionPart(chessboard.getPosition()) !== positionPart(nextState.fen)) {
      await chessboard.setPosition(nextState.fen, false);
    }
    renderMetadata(nextState);
  } catch (error) {
    message.textContent = error.message;
  }
}

async function closeGameWindow() {
  try {
    await request("/lifecycle/stop", {});
  } finally {
    window.close();
  }
}

document.querySelector("#new-game").addEventListener("click", resetGame);
difficulty.addEventListener("change", changeDifficulty);
togglePracticeButton.addEventListener("click", toggleMode);
document.querySelector("#return").addEventListener("click", closeGameWindow);
document.querySelector("#close-window").addEventListener("click", () => window.close());
refresh();
window.setInterval(refresh, 500);
