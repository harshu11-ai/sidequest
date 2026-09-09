// Runs the real app.js verbatim in a sandboxed context with a stubbed DOM,
// so we can drive renderMetadata() through a realistic sequence of polled
// states and assert on what the (stubbed) page would actually show, without
// needing a live, continuously-polling browser session. Invoked by
// tests/test_app_js.py as a subprocess; a non-zero exit means a failure.
"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const source = fs.readFileSync(process.argv[2], "utf8");

function makeElement(id) {
  return {id, hidden: true, textContent: "", value: "", title: "", disabled: false, classList: {
    _set: new Set(),
    toggle(name, on) { on ? this._set.add(name) : this._set.delete(name); },
    contains(name) { return this._set.has(name); },
  }, listeners: {}, addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }, click() {
    for (const fn of this.listeners.click || []) fn();
  }, closest() { return null; }};
}

const elements = {};
for (const id of [
  "board", "status", "engine", "difficulty", "message", "finished", "last-turn-notice",
  "side-note", "new-game", "practice-panel", "multiplayer-panel", "room-code",
  "opponent-dot", "opponent-status", "toggle-practice", "return", "close-window",
  "turn-dot",
]) {
  elements[id] = makeElement(id);
}

let closeCalled = 0;
const timeouts = [];

const sandbox = {
  console,
  window: {
    location: {search: ""},
    matchMedia: () => ({matches: false}),
    close: () => { closeCalled += 1; },
    setInterval: () => {},
    setTimeout: (fn, ms) => { timeouts.push(fn); },
  },
  document: {
    querySelector: (sel) => elements[sel.replace("#", "")] || makeElement(sel),
  },
  URLSearchParams: URLSearchParams,
  CMChessboard: {
    INPUT_EVENT_TYPE: {movingOverSquare: 1, moveInputStarted: 2, validateMoveInput: 3, moveInputFinished: 4},
    COLOR: {white: "w", black: "b"},
    BORDER_TYPE: {none: "none"},
    Chessboard: class {
      constructor() {}
      setPosition() { return Promise.resolve(); }
      setOrientation() {}
      getPosition() { return "startpos"; }
      isMoveInputEnabled() { return sandbox.__inputEnabled || false; }
      enableMoveInput() { sandbox.__inputEnabled = true; sandbox.__inputColor = arguments[1]; }
      disableMoveInput() { sandbox.__inputEnabled = false; sandbox.__inputColor = null; }
      removeMarkers() {}
      addMarker() {}
      removeLegalMovesMarkers() {}
    },
  },
  CMMarkers: {Markers: class {}, MARKER_TYPE: {square: "square"}},
  fetch: () => Promise.reject(new Error("fetch should not be called in this harness")),
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, {filename: "app.js"});

function assertHidden(id, expected, label) {
  assert.strictEqual(elements[id].hidden, expected, `${label}: #${id}.hidden should be ${expected}`);
}

// --- Stage 1: fresh practice game, agent actively thinking (window just opened) ---
sandbox.createBoard({
  fen: "startpos", legal_moves: [], status: "Your move", turn: "white", last_move: null,
  engine: "Sidequest practice bot", difficulty: "medium", mode: "practice", active: true,
});
assertHidden("last-turn-notice", true, "stage1");
assertHidden("finished", true, "stage1");
console.log("stage1 (active, mid-game): OK");

// --- Stage 2: agent finishes, but it's genuinely the player's move ---
sandbox.renderMetadata({
  fen: "startpos", legal_moves: [], status: "Your move", turn: "white", last_move: null,
  engine: "Sidequest practice bot", difficulty: "medium", mode: "practice", active: false,
});
assertHidden("last-turn-notice", false, "stage2");
assertHidden("finished", true, "stage2");
assert.strictEqual(closeCalled, 0, "stage2: window.close must not be scheduled yet");
console.log("stage2 (agent finished, your move): last-turn notice shown, window NOT closing -- OK");

// --- Stage 3: another poll tick while still waiting -- must stay stable, not flicker ---
sandbox.renderMetadata({
  fen: "startpos", legal_moves: [], status: "Your move", turn: "white", last_move: null,
  engine: "Sidequest practice bot", difficulty: "medium", mode: "practice", active: false,
});
assertHidden("last-turn-notice", false, "stage3");
assertHidden("finished", true, "stage3");
assert.strictEqual(closeCalled, 0, "stage3: still must not close");
console.log("stage3 (still waiting, repeated poll): stable -- OK");

// --- Stage 4: player takes their last move; practice bot replies inline,
//     handing the turn straight back to them -- must still close. ---
sandbox.awaitingLastTurn = true; // mirrors what submitMove() reads before posting
sandbox.renderMetadata({
  fen: "post-move-fen", legal_moves: [], status: "Your move", turn: "white", last_move: "e2e4",
  engine: "Sidequest practice bot", difficulty: "medium", mode: "practice", active: false,
  player_fen: "mid", computer_move: "e7e5",
});
sandbox.closeAfterFinish();
assertHidden("last-turn-notice", true, "stage4");
assertHidden("finished", false, "stage4");
assert.strictEqual(closeCalled, 0, "stage4: close is scheduled via setTimeout, not called synchronously");
assert.strictEqual(timeouts.length, 1, "stage4: exactly one close timeout scheduled");
timeouts.pop()();
assert.strictEqual(closeCalled, 1, "stage4: window.close should fire once the timeout runs");
console.log("stage4 (last move taken): finished overlay shown, window closes -- OK");

// --- Stage 5: multiplayer, agent finishes, but it's the OPPONENT's turn --
//     nothing for this player to do, so close immediately, no notice. ---
timeouts.length = 0;
closeCalled = 0;
sandbox.createBoard({
  fen: "startpos", legal_moves: [], status: "In progress", turn: "white", last_move: null,
  you: "black", your_turn: false, room_code: "ABC123", room_status: "in_progress", result: null,
  opponent_connected: true, engine: "Human opponent", difficulty: null, mode: "multiplayer",
  multiplayer: {room_code: "ABC123", you: "black", your_turn: false, room_status: "in_progress", opponent_connected: true, result: null},
  active: true,
});
sandbox.renderMetadata({
  fen: "startpos", legal_moves: [], status: "In progress", turn: "white", last_move: null,
  you: "black", your_turn: false, room_code: "ABC123", room_status: "in_progress", result: null,
  opponent_connected: true, engine: "Human opponent", difficulty: null, mode: "multiplayer",
  multiplayer: {room_code: "ABC123", you: "black", your_turn: false, room_status: "in_progress", opponent_connected: true, result: null},
  active: false,
});
assertHidden("last-turn-notice", true, "stage5");
assertHidden("finished", false, "stage5");
assert.strictEqual(timeouts.length, 1, "stage5: close scheduled immediately");
console.log("stage5 (not your turn): closes immediately, no notice shown -- OK");

// --- Stage 6: checkmate -- must close even if it's nominally "your" turn value ---
timeouts.length = 0;
closeCalled = 0;
sandbox.createBoard({
  fen: "startpos", legal_moves: [], status: "Your move", turn: "white", last_move: null,
  engine: "Sidequest practice bot", difficulty: "medium", mode: "practice", active: true,
});
sandbox.renderMetadata({
  fen: "mate-fen", legal_moves: [], status: "Checkmate", turn: "white", last_move: "d8h4",
  engine: "Sidequest practice bot", difficulty: "medium", mode: "practice", active: false,
});
assertHidden("last-turn-notice", true, "stage6");
assertHidden("finished", false, "stage6");
assert.strictEqual(timeouts.length, 1, "stage6: close scheduled on checkmate");
console.log("stage6 (checkmate): closes immediately, no notice -- OK");

// --- Stage 7: the earlier reported bug -- switching multiplayer(black) -> practice(white)
//     must re-register move input for White, not leave it stuck on Black. ---
sandbox.createBoard({
  fen: "startpos", legal_moves: ["e2e4"], status: "In progress", turn: "white", last_move: null,
  you: "black", your_turn: false, engine: "Human opponent", difficulty: null, mode: "multiplayer",
  multiplayer: {room_code: "ABC123", you: "black", your_turn: false, room_status: "in_progress", opponent_connected: true, result: null},
  active: true,
});
// It's White's move and this player is Black, so input should NOT be enabled yet.
assert.strictEqual(sandbox.__inputEnabled, false, "stage7: black player has no input while it's White's move");

sandbox.renderMetadata({
  fen: "after-e4", legal_moves: ["e7e5"], status: "In progress", turn: "black", last_move: "e2e4",
  you: "black", your_turn: true, engine: "Human opponent", difficulty: null, mode: "multiplayer",
  multiplayer: {room_code: "ABC123", you: "black", your_turn: true, room_status: "in_progress", opponent_connected: true, result: null},
  active: true,
});
assert.strictEqual(sandbox.__inputEnabled, true, "stage7: input enabled once it's Black's turn");
assert.strictEqual(sandbox.__inputColor, "b", "stage7: input registered for Black");

// Now switch to practice -- fresh game, this player is always White there.
sandbox.renderMetadata({
  fen: "startpos", legal_moves: ["e2e4"], status: "Your move", turn: "white", last_move: null,
  engine: "Sidequest practice bot", difficulty: "medium", mode: "practice", active: true,
  multiplayer: {room_code: "ABC123", you: "black", your_turn: false, room_status: "in_progress", opponent_connected: true, result: null},
});
assert.strictEqual(sandbox.__inputEnabled, true, "stage7: input still enabled in practice mode");
assert.strictEqual(
  sandbox.__inputColor, "w",
  "stage7 (the reported bug): input must be re-registered for White after switching to " +
  "practice, not left stuck on Black from the multiplayer game",
);
console.log("stage7 (black->practice input color bug): re-registers for White -- OK");

// --- Stage 8: the "play vs bot" button must actually be wired up. Regression
//     guard for a real shipped bug where toggleMode() existed but nothing
//     ever called it -- clicking the button silently did nothing. ---
assert.ok(
  (elements["toggle-practice"].listeners.click || []).length > 0,
  "stage8: #toggle-practice must have a click listener attached (toggleMode wired up)",
);
elements["toggle-practice"].click();
(async () => {
  // toggleMode() is async; let its rejected fetch() unwind before asserting.
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(
    elements.message.textContent.length > 0,
    "stage8: clicking the button must actually invoke toggleMode() (expected it to reach " +
    "the stubbed fetch and surface an error, got no message at all)",
  );
  console.log("stage8 (play-vs-bot button click is wired to toggleMode): OK");

  console.log("\nAll last-turn and input-color state-machine assertions passed.");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
