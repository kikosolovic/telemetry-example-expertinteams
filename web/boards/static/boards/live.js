// Live stability view: WebRTC client for the `realtime` service.
//
// Flow (SCRUM-64/79/152, spike Q1):
//   1. Ask Django for a short-lived signed viewer token.
//   2. Create an RTCPeerConnection with two data channels BEFORE the offer:
//        events    - reliable + ordered (default): session events, latency reports
//        stability - ordered:false, maxRetransmits:0: the ~100 Hz live samples.
//                    A late sample is worthless (a newer one is already on its
//                    way), so we never want SCTP to hold back newer messages
//                    while it retransmits an old one (head-of-line blocking).
//   3. Wait for ICE gathering to finish, then POST the complete offer. aiortc
//      also returns a complete answer, so there is no trickle ICE and no
//      WebSocket -- that's why Django Channels is not needed.
//   4. Draw on requestAnimationFrame: messages arrive every ~10 ms but the
//      screen refreshes every ~16 ms, so we only keep the newest sample and
//      draw once per frame.
(() => {
  "use strict";
  const C = window.LIVE;
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- state
  let pc = null;
  let events = null;
  let latest = null;         // newest stability message not yet drawn
  let lastDrawnSeq = -1;     // highest seq drawn (for the latency echo)
  let lastSeqSeen = -1;      // highest seq received (for dropping stale ones)
  let lastBoot = null;       // board boot id: a reboot restarts seq at 0
  let lastMsgAt = 0;         // performance.now() of the last stability message
  let msgCount = 0, staleCount = 0;
  let latency = null;        // last report from the server
  let reconnectTimer = null;
  const trail = [];          // CoP history for the trail (last ~1 s)
  const scores = [];         // score history for the sparkline (last 30 s)

  // ---------------------------------------------------------------- connect
  async function connect() {
    clearTimeout(reconnectTimer);
    setConn("connecting…", "");
    try {
      const { token } = await (await fetch(C.tokenUrl, { credentials: "same-origin" })).json();

      pc = new RTCPeerConnection({ iceServers: C.iceServers });
      events = pc.createDataChannel("events");                      // reliable, ordered
      const stability = pc.createDataChannel("stability", { ordered: false, maxRetransmits: 0 });

      stability.onmessage = (e) => onStability(JSON.parse(e.data));
      events.onmessage = (e) => onEvent(JSON.parse(e.data));
      events.onopen = () => setConn("live", "ok");

      pc.onconnectionstatechange = () => {
        log(`peer connection: ${pc.connectionState}`);
        if (["failed", "disconnected", "closed"].includes(pc.connectionState)) scheduleReconnect();
      };

      await pc.setLocalDescription(await pc.createOffer());
      await iceGatheringComplete(pc);

      const res = await fetch(C.offerUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type, token }),
      });
      if (!res.ok) throw new Error(`offer rejected: ${res.status} ${await res.text()}`);
      await pc.setRemoteDescription(await res.json());
    } catch (err) {
      log(`connect failed: ${err.message}`);
      scheduleReconnect();
    }
  }

  function iceGatheringComplete(pc) {
    // Non-trickle: send the offer once all candidates are in it. The timeout
    // stops a slow STUN server from blocking the connection forever.
    if (pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => pc.iceGatheringState === "complete" && resolve();
      pc.addEventListener("icegatheringstatechange", done);
      setTimeout(resolve, 2000);
    });
  }

  function scheduleReconnect() {
    // SCRUM-64: clients reconnect on their own. If `realtime` restarts, every
    // peer is lost; this brings the page back without a reload.
    setConn("reconnecting…", "warn");
    if (pc) { try { pc.close(); } catch (_) {} pc = null; }
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2000);
  }

  // ---------------------------------------------------------------- messages
  function onStability(m) {
    // Message format (SCRUM-80 type + payload, spike Q1):
    // {"type":"stability","v":1,"seq":..,"t":ms,"cop":[x,y],"roll":..,"pitch":..,"score":..}
    if (m.type !== "stability") return;
    if (m.boot !== lastBoot) { lastBoot = m.boot; lastSeqSeen = -1; }  // board rebooted
    // Unordered channel: an older sample may arrive after a newer one.
    // Drawing it would make the dot jump backwards, so drop it.
    if (m.seq <= lastSeqSeen) { staleCount++; return; }
    lastSeqSeen = m.seq;
    lastMsgAt = performance.now();
    msgCount++;
    latest = m;
  }

  function onEvent(m) {
    switch (m.type) {
      case "latency": latency = m; break;
      case "hello": log(`joined session ${m.session} · board ${m.device_online ? "online" : "offline"}`); break;
      case "board": log(`board ${m.online ? "online" : "offline"}${m.online ? "" : " (Wi-Fi drop?)"}`); break;
      case "session_ended": log("session ended by trainer"); setConn("ended", ""); break;
      default: log(JSON.stringify(m));
    }
  }

  // Once a second, tell the server which seq we just drew. The server knows
  // when it sent that seq, so (now - sent) / 2 ~ server -> screen latency,
  // including the wait for the next animation frame (spike "How to measure").
  // Only echo a seq we haven't echoed yet: if the stream stalls, re-echoing
  // the same old seq would make the latency look like it grows every second.
  let lastEchoedSeq = -1;
  setInterval(() => {
    if (events && events.readyState === "open" && lastDrawnSeq > lastEchoedSeq) {
      lastEchoedSeq = lastDrawnSeq;
      events.send(JSON.stringify({ type: "echo", seq: lastDrawnSeq, boot: lastBoot }));
    }
  }, 1000);

  // Messages-per-second counter
  setInterval(() => { $("rate").textContent = msgCount; msgCount = 0; $("stale").textContent = staleCount; }, 1000);

  // ---------------------------------------------------------------- drawing
  const cop = $("cop").getContext("2d");
  const spark = $("spark").getContext("2d");
  const css = getComputedStyle(document.documentElement);
  const color = (n) => css.getPropertyValue(n).trim();

  function frame() {
    if (latest) {
      const m = latest;
      latest = null;
      lastDrawnSeq = m.seq;
      trail.push(m.cop);
      if (trail.length > 60) trail.shift();          // ~1 s at 60 fps
      scores.push(m.score);
      if (scores.length > 1800) scores.shift();      // ~30 s at 60 fps
      $("score").textContent = Math.round(m.score);
      $("roll").textContent = `${m.roll.toFixed(2)}°`;
      $("pitch").textContent = `${m.pitch.toFixed(2)}°`;
      $("copxy").textContent = `${m.cop[0].toFixed(1)}, ${m.cop[1].toFixed(1)} mm`;
      $("seq").textContent = m.seq;
      drawCop();
      drawSpark();
    }
    updateWarnings();
    requestAnimationFrame(frame);
  }

  function drawCop() {
    const W = cop.canvas.width, H = cop.canvas.height, half = 200;  // ±200 mm
    const px = (x) => W / 2 + (x / half) * (W / 2 - 10);
    const py = (y) => H / 2 - (y / half) * (H / 2 - 10);           // +y = front = up
    cop.clearRect(0, 0, W, H);
    // board outline, quadrant lines and a 50 mm target circle
    cop.strokeStyle = color("--line");
    cop.lineWidth = 1;
    cop.strokeRect(10, 10, W - 20, H - 20);
    cop.beginPath(); cop.moveTo(W / 2, 10); cop.lineTo(W / 2, H - 10); cop.moveTo(10, H / 2); cop.lineTo(W - 10, H / 2); cop.stroke();
    cop.beginPath(); cop.arc(W / 2, H / 2, (50 / half) * (W / 2 - 10), 0, 2 * Math.PI);
    cop.fillStyle = color("--target"); cop.fill();
    // trail
    cop.strokeStyle = color("--teal"); cop.lineWidth = 2; cop.globalAlpha = 0.5;
    cop.beginPath();
    trail.forEach(([x, y], i) => (i ? cop.lineTo(px(x), py(y)) : cop.moveTo(px(x), py(y))));
    cop.stroke(); cop.globalAlpha = 1;
    // current point
    const [x, y] = trail[trail.length - 1];
    cop.fillStyle = color("--berry");
    cop.beginPath(); cop.arc(px(x), py(y), 8, 0, 2 * Math.PI); cop.fill();
  }

  function drawSpark() {
    const W = spark.canvas.width, H = spark.canvas.height;
    spark.clearRect(0, 0, W, H);
    spark.strokeStyle = color("--teal"); spark.lineWidth = 2; spark.setLineDash([5, 3]);  // stability = teal dashed (SCRUM-198)
    spark.beginPath();
    scores.forEach((s, i) => {
      const x = (i / 1799) * W, y = H - (s / 100) * (H - 4) - 2;
      i ? spark.lineTo(x, y) : spark.moveTo(x, y);
    });
    spark.stroke(); spark.setLineDash([]);
  }

  function updateWarnings() {
    const w = $("warning");
    const silentMs = lastMsgAt ? performance.now() - lastMsgAt : 0;
    if (latency) {
      $("lat-bs").textContent = `${latency.board_server_p50_ms.toFixed(1)} ms p50 · ${latency.board_server_p95_ms.toFixed(1)} p95`;
      $("lat-sc").textContent = latency.server_client_ms == null ? "–" : `${latency.server_client_ms.toFixed(1)} ms`;
      $("lat-total").textContent = latency.total_ms == null ? "–" : `${latency.total_ms.toFixed(1)} ms`;
      $("lat-offset").textContent = latency.clock_offset_ms == null ? "not synced" : `${latency.clock_offset_ms.toFixed(1)} ms`;
    }
    let msg = "";
    if (lastMsgAt && silentMs > 1000) msg = `Live connection lost – no data for ${(silentMs / 1000).toFixed(0)} s. Data is still being buffered by the board and stored via TCP.`;
    else if (latency && latency.total_ms > 500) msg = `High latency: ${latency.total_ms.toFixed(0)} ms – live feedback is delayed.`;
    w.hidden = !msg;
    w.textContent = msg;
  }

  // ---------------------------------------------------------------- stored record (Django)
  async function pollStatus() {
    try {
      const s = await (await fetch(C.statusUrl, { credentials: "same-origin" })).json();
      $("db-samples").textContent = s.stats.samples;
      $("db-gaps").textContent = s.gaps.length
        ? s.gaps.map((g) => `${g.from}–${g.to}`).join(", ") + (s.gaps_final ? " (final)" : "")
        : (s.gaps_final ? "none (final)" : "none");
    } catch (_) { /* Django down: ignore, live view keeps working */ }
  }
  setInterval(pollStatus, 2000);

  // ---------------------------------------------------------------- helpers
  function setConn(text, cls) { const el = $("conn"); el.textContent = text; el.className = `pill ${cls}`; }
  function log(text) {
    const el = $("log");
    el.textContent = `${new Date().toLocaleTimeString()}  ${text}\n` + el.textContent.slice(0, 4000);
  }

  pollStatus();
  connect();
  requestAnimationFrame(frame);
})();
