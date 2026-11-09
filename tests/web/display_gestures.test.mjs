// The clock display's touch handling - see voice_assistant/web/display-gestures.js.
//
// A touch is given as the points it passed through, from the finger going down to it
// lifting, each with the milliseconds it was seen at.

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const { classify, isHold } = require("../../voice_assistant/web/display-gestures.js");

// A finger held still for a while, as {x, y, t} from start to end.
function still(ms) {
  return [{ x: 400, y: 240, t: 0 }, { x: 402, y: 241, t: ms / 2 }, { x: 401, y: 240, t: ms }];
}

// A finger moved sideways by dx pixels over ms milliseconds.
function moved(dx, ms) {
  return [{ x: 400, y: 240, t: 0 }, { x: 400 + dx / 2, y: 242, t: ms / 2 }, { x: 400 + dx, y: 243, t: ms }];
}

test("a short still touch is a tap", () => {
  assert.equal(classify(still(100)), "tap");
});

test("a long still touch is a hold, decided once it's long enough", () => {
  assert.equal(classify(still(500)), "hold");
  const down = [{ x: 400, y: 240, t: 0 }, { x: 402, y: 241, t: 100 }];
  assert.equal(isHold(down, 300), false);
  assert.equal(isHold(down, 460), true);
});

test("a quick sideways move is a swipe", () => {
  assert.equal(classify(moved(-80, 200)), "swipe-left");
  assert.equal(classify(moved(80, 200)), "swipe-right");
});

test("a short move is nothing", () => {
  assert.equal(classify(moved(30, 200)), "none");
});

test("a long touch which moved is never a hold", () => {
  const wandered = moved(40, 500);
  assert.equal(classify(wandered), "none");
  assert.equal(isHold(wandered, 500), false);
});
