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
  "side-note", "new-game", "practice-panel", "return", "close-window", "turn-dot",
]) {
  elements[id] = makeElement(id);
}

const timeouts = [];
// The window persists across turns now -- finishing a turn asks the server
// to collapse it (POST /api/breaks/collapse) instead of calling
// window.close(). fetchCalls records every fetch() the page makes so the
// stages below can assert a collapse was actually requested; the promise
// itself rejects (as before) since nothing here awaits its result.
const fetchCalls = [];

const sandbox = {
  console,
  window: {
    location: {search: ""},
    matchMedia: () => ({matches: false}),
    close: () => { throw new Error("window.close() should not be called -- the window persists now"); },
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
  fetch: (url, options) => {
    fetchCalls.push({url, options});
    return Promise.reject(new Error("fetch should not be awaited by this harness"));
  },
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, {filename: "app.js"});

function assertHidden(id, expected, label) {
  assert.strictEqual(elements[id].hidden, expected, `${label}: #${id}.hidden should be ${expected}`);
}

// refresh()'s own polling fetch() runs unconditionally at load and on every
// tick -- only collapse requests are what these stages care about.
function collapseCalls() {
  return fetchCalls.filter((call) => call.url.startsWith("/api/breaks/collapse"));
}

// --- Stage 1: fresh practice game, agent actively thinking (window just opened) ---
sandbox.createBoard({
  fen: "startpos", legal_moves: [], status: "Your move", turn: "white", last_move: null,
  engine: "Sidequest practice bot", difficulty: "medium", active: true,
});
assertHidden("last-turn-notice", true, "stage1");
assertHidden("finished", true, "stage1");
console.log("stage1 (active, mid-game): OK");

// --- Stage 2: agent finishes, but it's genuinely the player's move ---
sandbox.renderMetadata({
  fen: "startpos", legal_moves: [], status: "Your move", turn: "white", last_move: null,
  engine: "Sidequest practice bot", difficulty: "medium", active: false,
});
assertHidden("last-turn-notice", false, "stage2");
assertHidden("finished", true, "stage2");
assert.strictEqual(collapseCalls().length, 0, "stage2: nothing should be collapsed yet");
console.log("stage2 (agent finished, your move): last-turn notice shown, window NOT collapsing -- OK");

// --- Stage 3: another poll tick while still waiting -- must stay stable, not flicker ---
sandbox.renderMetadata({
  fen: "startpos", legal_moves: [], status: "Your move", turn: "white", last_move: null,
  engine: "Sidequest practice bot", difficulty: "medium", active: false,
});
assertHidden("last-turn-notice", false, "stage3");
assertHidden("finished", true, "stage3");
assert.strictEqual(collapseCalls().length, 0, "stage3: still must not collapse");
console.log("stage3 (still waiting, repeated poll): stable -- OK");

// --- Stage 4: player takes their last move; the bot replies inline, handing
//     the turn straight back to them -- must still close. ---
sandbox.awaitingLastTurn = true; // mirrors what submitMove() reads before posting
sandbox.renderMetadata({
  fen: "post-move-fen", legal_moves: [], status: "Your move", turn: "white", last_move: "e2e4",
  engine: "Sidequest practice bot", difficulty: "medium", active: false,
  player_fen: "mid", computer_move: "e7e5",
});
sandbox.closeAfterFinish();
assertHidden("last-turn-notice", true, "stage4");
assertHidden("finished", false, "stage4");
assert.strictEqual(collapseCalls().length, 0, "stage4: collapse is scheduled via setTimeout, not requested synchronously");
assert.strictEqual(timeouts.length, 1, "stage4: exactly one collapse timeout scheduled");
timeouts.pop()();
assert.strictEqual(collapseCalls().length, 1, "stage4: a collapse request should fire once the timeout runs");
assert.strictEqual(collapseCalls()[0].options.method, "POST");
console.log("stage4 (last move taken): finished overlay shown, collapse requested -- OK");

// --- Stage 5: checkmate -- must collapse even though it's nominally "your" turn ---
timeouts.length = 0;
fetchCalls.length = 0;
sandbox.createBoard({
  fen: "startpos", legal_moves: [], status: "Your move", turn: "white", last_move: null,
  engine: "Sidequest practice bot", difficulty: "medium", active: true,
});
sandbox.renderMetadata({
  fen: "mate-fen", legal_moves: [], status: "Checkmate", turn: "white", last_move: "d8h4",
  engine: "Sidequest practice bot", difficulty: "medium", active: false,
});
assertHidden("last-turn-notice", true, "stage5");
assertHidden("finished", false, "stage5");
assert.strictEqual(timeouts.length, 1, "stage5: collapse scheduled on checkmate");
console.log("stage5 (checkmate): collapses immediately, no notice -- OK");

console.log("\nAll last-turn state-machine assertions passed.");
