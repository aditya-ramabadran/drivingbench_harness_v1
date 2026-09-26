"use strict";

// The generated prompt: fixed wording, with the objective and shared car settings filled in.
window.DrivingBenchPrompt = (() => {
  const IMAGE_PRESETS = {
    neutral: { exposure_ev: 0, contrast: 1, saturation: 1 },
    night: { exposure_ev: 1.2, contrast: 1.18, saturation: 1.18 },
  };
  function buildPrompt(saved) {
    return [
      "Use only the `drivingbench_sandbox` MCP. Complete the stated objective using its tools.",
      "",
      "OBJECTIVE",
      saved.objective || "No objective entered.",
      "",
      "ROLE",
      "Use set_motion(direction, steering_percent, speed_mps, duration_s, reason). Direction is left, right or straight; steering_percent is 0–100. Zero is straight for either turn direction.",
      `100% requests ${saved.max_steering_angle_deg} steering-wheel degrees; observe reports steering_percent on the same scale, positive left. The speed ceiling is ${saved.speed_limit_mps} m/s. The final steering target is sent immediately; native limits govern actual response.`,
      "In every set_motion and stop_now, give reason: about thirty words on what you see and what the command is for. It is recorded for the operator and never changes what the car does.",
      "",
      "",
      "DRIVING",
      "- Choose durations that allow observation and reasoning latency; the car continues moving while you think.",
      "- Observe, send a motion command, then observe actual movement and adapt.",
      "- Commands replace globally rather than queue. Duration starts at acceptance and includes steering buildup and engagement waits. Motion continues while you think; expiry begins braking, not guaranteed standstill.",
      "- A rejected replacement leaves the previous command unchanged. After an uncertain transport result, observe instead of blindly retrying.",
      "- Any connected chat can replace the active command or call stop_now. DrivingBench is always active. Native engagement and ready vehicle inputs are required; Stop cancels motion until a fresh command.",
      "- timestamp is UTC response time, not image capture or command acceptance time. image_age_s is image age at producer response.",
      "- Carefully look for objects and obstacles (parked cars, buildings, islands, trees, etc); they’re all 3D and the vehicle you are controlling is also 3D and is also wider than it might seem from the camera images. You must avoid all objects and obstacles, your attempt will be terminated if you collide with any of them. ",
      "- If possible, try to always be in the center of your lane/road and maximize distance from obstacles.",
      "- In successive observed images, pay attention to what changes, i.e. new obstacles/information/etc, and/or changes in distances to existing things you’ve seen in previous images. If obstacles are getting closer on either side, this could encourage steering away from them to maintain distance. ",
      "- Over time, understand the behavior that occurs when you choose different actions or steering inputs and adapt to this. ",
      "- Think and plan carefully where you want to end up and plan your route accordingly and advance toward subgoals. ",
      "- Don’t be afraid to do sharp turns or choose high steering %s. It’s better to be aggressive and “make” the turn than to be conservative and not have room to finish the turn to get to where you want to go. ",
      "- Also, your vehicle cannot do arbitrarily tight turns; 100% might be much less tight than you expect. 100% corresponds to doing a 90 degree turn at 1 m/s for ~60 seconds. Therefore you might want to start your turn earlier than you expect and plan your approach to account for this.",
      "- Largely keep your turning %s for left/right in the set {30%, 60%, 100%}.",
    ].join("\n");
  }
  return { buildPrompt, IMAGE_PRESETS };
})();
