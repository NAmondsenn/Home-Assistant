// What a touch on the clock display was - see docs/decisions/0013, decisions 3 and 4.
//
// A touch is the points the finger passed through, from going down to lifting, each as
// {x, y, t} in pixels and milliseconds. A tap or a swipe changes the page; a hold makes
// Nova listen, so it has to be told apart from both with room to spare - a tap that
// lingered, or a swipe that started slowly, must never be taken for one.
//
// A plain script rather than a module, so the page loads it with a <script> tag as it
// is, and the tests require() it in Node.

"use strict";

(function (root) {
  // How long a finger must stay still before it's a hold, in milliseconds - long
  // enough that a slow tap isn't one, short enough not to feel like waiting.
  const HOLD_MS = 450;

  // The longest a still touch can last and still be a tap. One between this and
  // HOLD_MS is nothing at all, so a hesitant tap can't land on either side by chance.
  const TAP_MS = 300;

  // How far a finger can wander, in pixels, and still count as still - a fingertip
  // rolls a little on glass even when it means to stay put.
  const STILL_PX = 15;

  // How far sideways a swipe must go, and how quickly, in pixels and milliseconds.
  const SWIPE_PX = 60;
  const SWIPE_MS = 600;

  // The furthest any point got from where the finger went down, in pixels.
  function furthest(points) {
    const first = points[0];
    let most = 0;
    for (const point of points) {
      most = Math.max(most, Math.hypot(point.x - first.x, point.y - first.y));
    }
    return most;
  }

  // Whether a finger still down is a hold by nowMs - decided while it's down, so Nova
  // starts listening as the hold begins rather than when the finger lifts.
  function isHold(points, nowMs) {
    if (!points.length) return false;
    return nowMs - points[0].t >= HOLD_MS && furthest(points) <= STILL_PX;
  }

  // What a finished touch was: "tap", "hold", "swipe-left", "swipe-right" or "none".
  // "swipe-left" is the finger moving leftwards, as when turning to the next page.
  function classify(points) {
    if (!points.length) return "none";
    const first = points[0];
    const last = points[points.length - 1];
    const lasted = last.t - first.t;
    if (furthest(points) <= STILL_PX) {
      if (lasted >= HOLD_MS) return "hold";
      return lasted <= TAP_MS ? "tap" : "none";
    }
    const dx = last.x - first.x;
    const dy = last.y - first.y;
    // Mostly sideways, so a drag down the screen isn't taken for a page turn.
    if (lasted <= SWIPE_MS && Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > Math.abs(dy)) {
      return dx < 0 ? "swipe-left" : "swipe-right";
    }
    return "none";
  }

  const gestures = { classify, isHold, HOLD_MS, TAP_MS, STILL_PX, SWIPE_PX, SWIPE_MS };
  if (typeof module !== "undefined" && module.exports) module.exports = gestures;
  if (root) root.NovaGestures = gestures;
})(typeof window !== "undefined" ? window : null);
