/*
 * app.js — the web Body's page.
 *
 * Two modes, chosen by how the page was loaded:
 *
 *   file:// or ?mock   → the UI alone, driven by a scripted fake. This is
 *                        how the look is reviewed without a backend, and
 *                        it is also the honest fallback if the Body is
 *                        down: the face still renders.
 *   http://            → the real thing. SSE for state and narration,
 *                        MJPEG for the feed, POST /ask for questions.
 *
 * Everything the page does with audio stays on the path the desktop Body
 * already proved: 16 kHz mono 16-bit WAV in, plain text out. The browser
 * replaces sounddevice and pyttsx3 — those two, not the protocol.
 */
(function () {
  "use strict";

  var MOCK =
    location.protocol === "file:" ||
    new URLSearchParams(location.search).has("mock");

  var SAMPLE_RATE = 16000; // what perception/voice.py produces and Gemini expects
  var MAX_RECORD_MS = 30000;

  var el = {
    orb: document.getElementById("orb"),
    orbWrap: document.getElementById("orb-wrap"),
    orbHint: document.getElementById("orb-hint"),
    stateChip: document.getElementById("state-chip"),
    linkChip: document.getElementById("link-chip"),
    cameraPanel: document.getElementById("camera-panel"),
    camFeed: document.getElementById("cam-feed"),
    camMock: document.getElementById("cam-mock"),
    camEmpty: document.getElementById("cam-empty"),
    camRes: document.getElementById("cam-res"),
    camFps: document.getElementById("cam-fps"),
    youText: document.getElementById("you-text"),
    auraText: document.getElementById("aura-text"),
    ambientText: document.getElementById("ambient-text"),
    ambientTag: document.getElementById("ambient-tag"),
    btnTalk: document.getElementById("btn-talk"),
    btnCam: document.getElementById("btn-cam"),
    toast: document.getElementById("toast")
  };

  var orb = new Orb(el.orb);
  orb.start();

  var state = "observing";
  var cameraOpen = false;
  var recording = false;
  var busy = false; // an exchange is in flight; ignore further input

  // ---------------------------------------------------------------- state

  function setState(next) {
    if (state === next) return;
    state = next;
    orb.setState(next);
    el.stateChip.textContent = next;
  }

  function setLink(kind, label) {
    el.linkChip.dataset.link = kind;
    el.linkChip.textContent = label;
  }

  function setYou(text, pending) {
    el.youText.textContent = text || "—";
    el.youText.classList.toggle("pending", !!pending);
  }

  function setAura(text, pending) {
    el.auraText.textContent = text || "—";
    el.auraText.classList.toggle("pending", !!pending);
  }

  function setAmbient(text, suppressed) {
    el.ambientText.textContent = text || "—";
    el.ambientTag.hidden = !suppressed;
  }

  var toastTimer = 0;
  function toast(message, isError) {
    el.toast.textContent = message;
    el.toast.classList.toggle("error", !!isError);
    el.toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.toast.classList.remove("show");
    }, 6000);
  }

  // --------------------------------------------------------------- speech

  var voice = null;
  function pickVoice() {
    if (!window.speechSynthesis) return;
    var voices = window.speechSynthesis.getVoices();
    if (!voices.length) return;
    // Prefer a female-sounding English voice; fall back to any English.
    voice =
      voices.find(function (v) {
        return /en/i.test(v.lang) && /female|zira|samantha|aria|jenny/i.test(v.name);
      }) ||
      voices.find(function (v) { return /en/i.test(v.lang); }) ||
      voices[0];
  }
  if (window.speechSynthesis) {
    pickVoice();
    window.speechSynthesis.onvoiceschanged = pickVoice;
  }

  /*
   * Speaks `text` in the page. Returns a promise that settles when the
   * utterance ends, so the caller can hold the orb in `speaking` for
   * exactly as long as sound is coming out rather than for a guess.
   */
  function speak(text) {
    if (!window.speechSynthesis || !text) return Promise.resolve();

    return new Promise(function (resolve) {
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(text);
      if (voice) u.voice = voice;
      u.rate = 1.0;
      u.pitch = 1.05;
      u.onboundary = function () { orb.pulse(); }; // one ring per word
      u.onend = resolve;
      u.onerror = resolve;
      window.speechSynthesis.speak(u);
    });
  }

  // ------------------------------------------------------------ recording

  var rec = null; // { stream, ctx, node, analyser, chunks, startedAt, data }

  function stopTracks() {
    if (rec && rec.stream) {
      rec.stream.getTracks().forEach(function (t) { t.stop(); });
    }
  }

  async function startRecording() {
    if (rec) return;

    var stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      }
    });

    var ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
    if (ctx.state === "suspended") await ctx.resume();

    await ctx.audioWorklet.addModule("recorder-worklet.js");

    var source = ctx.createMediaStreamSource(stream);
    var node = new AudioWorkletNode(ctx, "aura-recorder");
    var analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    source.connect(node);
    // A worklet with no downstream connection is not pulled by some
    // browsers, so it is wired to a muted gain rather than to the
    // speakers — routing the mic to the output would be a feedback loop.
    var sink = ctx.createGain();
    sink.gain.value = 0;
    node.connect(sink).connect(ctx.destination);

    var chunks = [];
    node.port.onmessage = function (e) { chunks.push(e.data); };

    rec = {
      stream: stream,
      ctx: ctx,
      node: node,
      analyser: analyser,
      sink: sink,
      chunks: chunks,
      startedAt: performance.now(),
      buf: new Uint8Array(analyser.fftSize)
    };

    setState("listening");
    setYou("listening…", true);
    pumpMeter();

    // A recording left running holds the exchange open, which suppresses
    // narration for as long as it lasts. Cap it, and send what there is
    // rather than discarding it.
    rec.limit = setTimeout(function () {
      if (recording) {
        toast("Recording hit the 30 second limit — sending it now.");
        talk();
      }
    }, MAX_RECORD_MS);
  }

  function pumpMeter() {
    if (!rec || !recording) return;
    rec.analyser.getByteTimeDomainData(rec.buf);
    var peak = 0;
    for (var i = 0; i < rec.buf.length; i++) {
      peak = Math.max(peak, Math.abs(rec.buf[i] - 128) / 128);
    }
    orb.setAmplitude(peak);
    requestAnimationFrame(pumpMeter);
  }

  async function stopRecording() {
    if (!rec) return null;
    var r = rec;

    clearTimeout(r.limit);
    var blocks = r.chunks;
    stopTracks();
    try { r.node.disconnect(); } catch (e) { /* already gone */ }
    try { r.sink.disconnect(); } catch (e) { /* already gone */ }
    await r.ctx.close();
    rec = null;
    orb.setAmplitude(0);

    var length = blocks.reduce(function (n, b) { return n + b.length; }, 0);
    if (!length) return null;

    var all = new Float32Array(length);
    var off = 0;
    blocks.forEach(function (b) { all.set(b, off); off += b.length; });

    var samples = r.ctx.sampleRate === SAMPLE_RATE ? all : resample(all, r.ctx.sampleRate, SAMPLE_RATE);
    return encodeWav(samples, SAMPLE_RATE);
  }

  /* Linear resampling, used only when the browser refused the 16 kHz
   * AudioContext. Correct enough for speech, and it keeps the WAV we
   * send in the one format the transcriber has been proven against. */
  function resample(input, from, to) {
    var ratio = from / to;
    var out = new Float32Array(Math.floor(input.length / ratio));
    for (var i = 0; i < out.length; i++) {
      var pos = i * ratio;
      var i0 = Math.floor(pos);
      var i1 = Math.min(i0 + 1, input.length - 1);
      out[i] = input[i0] + (input[i1] - input[i0]) * (pos - i0);
    }
    return out;
  }

  function encodeWav(samples, rate) {
    var buffer = new ArrayBuffer(44 + samples.length * 2);
    var view = new DataView(buffer);

    function str(offset, text) {
      for (var i = 0; i < text.length; i++) {
        view.setUint8(offset + i, text.charCodeAt(i));
      }
    }

    str(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    str(8, "WAVE");
    str(12, "fmt ");
    view.setUint32(16, 16, true); // PCM header size
    view.setUint16(20, 1, true); // format: PCM
    view.setUint16(22, 1, true); // channels: mono
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true); // byte rate
    view.setUint16(32, 2, true); // block align
    view.setUint16(34, 16, true); // bits per sample
    str(36, "data");
    view.setUint32(40, samples.length * 2, true);

    var off = 44;
    for (var i = 0; i < samples.length; i++, off += 2) {
      var s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([view], { type: "audio/wav" });
  }

  // ------------------------------------------------------------- exchange

  // ------------------------------------------------------------- exchange

  /*
   * The server holds an "exchange is in progress" flag from before the
   * recording starts until AURA has finished speaking here in the page —
   * during which ambient narration is dropped rather than queued. The
   * recording and the speaking both happen in the browser, so the page
   * has to open and close that window itself; the server cannot tell when
   * an utterance ends.
   *
   * If the page dies mid-exchange the flag would stay set and narration
   * would be suppressed for the rest of the session, so the server
   * expires it after two minutes on its own. These calls make that the
   * fallback rather than the normal path.
   */
  function beginExchange() {
    if (MOCK) return Promise.resolve();
    return fetch("/exchange/begin", { method: "POST" }).catch(function () {});
  }

  function endExchange() {
    if (MOCK) return Promise.resolve();
    return fetch("/exchange/end", { method: "POST" }).catch(function () {});
  }

  async function ask(wav) {
    var response = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wav
    });
    var payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      throw new Error(payload.error || "the server returned " + response.status);
    }
    return payload;
  }

  async function talk() {
    if (busy) return;

    if (recording) {
      recording = false;
      busy = true;
      el.btnTalk.setAttribute("aria-pressed", "false");
      setState("thinking");
      setYou("transcribing…", true);

      try {
        var wav = await stopRecording();
        if (!wav) throw new Error("nothing was recorded");

        var result = MOCK ? await mockAnswer() : await ask(wav);

        if (result.transcript) setYou(result.transcript, false);
        else setYou("(heard nothing usable)", true);

        setAura(result.text, false);
        setState("speaking");
        await speak(result.text);
      } catch (err) {
        toast(String(err.message || err), true);
        setYou("—", false);
      } finally {
        await endExchange();
        busy = false;
        setState("observing");
      }
      return;
    }

    try {
      setYou("—", false);
      setAura("—", false);
      setAmbient("—", false);
      await beginExchange();
      recording = true;
      el.btnTalk.setAttribute("aria-pressed", "true");
      await startRecording();
    } catch (err) {
      recording = false;
      await endExchange();
      setState("observing");
      toast(
        "Microphone unavailable: " + (err.message || err) +
        " — the page must be opened at 127.0.0.1, not an IP address.",
        true
      );
    }
  }

  // --------------------------------------------------------------- camera

  function setCamera(open) {
    cameraOpen = open;
    document.body.classList.toggle("camera-open", open);
    el.cameraPanel.setAttribute("aria-hidden", String(!open));
    el.btnCam.setAttribute("aria-pressed", String(open));

    if (open) {
      if (MOCK) {
        startMockCamera();
      } else {
        // MJPEG: the browser keeps the connection open and repaints the
        // <img> on every part. Nothing to drive from JavaScript.
        el.camEmpty.hidden = true;
        el.camMock.hidden = true;
        el.camFeed.src = "/stream.mjpg?t=" + Date.now();
      }
    } else {
      el.camFeed.removeAttribute("src");
      stopMockCamera();
    }
  }

  function toggleCamera() {
    setCamera(!cameraOpen);
  }

  // ------------------------------------------------------------ mock mode

  var mock = { raf: 0, t: 0, phase: 0, timer: 0 };
  var MOCK_ANSWER =
    "I can see the desk in front of me — though nothing here is real " +
    "yet, this is the interface running on its own.";

  function startMockCamera() {
    var c = el.camMock;
    c.hidden = false;
    el.camEmpty.hidden = true;

    function resize() {
      var dpr = window.devicePixelRatio || 1;
      var rect = c.getBoundingClientRect();
      c.width = Math.round(rect.width * dpr);
      c.height = Math.round(rect.height * dpr);
      var g = c.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      mock.w = rect.width;
      mock.h = rect.height;
    }
    resize();
    window.addEventListener("resize", resize);

    function frame() {
      mock.t += 0.016;
      var g = c.getContext("2d");
      var w = mock.w;
      var h = mock.h;

      g.fillStyle = "#05080e";
      g.fillRect(0, 0, w, h);

      var grad = g.createRadialGradient(
        w * (0.5 + Math.sin(mock.t * 0.3) * 0.16),
        h * (0.5 + Math.cos(mock.t * 0.23) * 0.16),
        0, w / 2, h / 2, Math.max(w, h) * 0.7
      );
      grad.addColorStop(0, "rgba(56,232,255,0.16)");
      grad.addColorStop(1, "rgba(5,8,14,0)");
      g.fillStyle = grad;
      g.fillRect(0, 0, w, h);

      var boxes = [
        { x: 0.18, y: 0.3, w: 0.2, h: 0.34, label: "person" },
        { x: 0.55, y: 0.44, w: 0.16, h: 0.2, label: "cup" },
        { x: 0.62, y: 0.72, w: 0.24, h: 0.16, label: "laptop" }
      ];
      boxes.forEach(function (b, i) {
        var drift = Math.sin(mock.t * 0.6 + i) * 0.01;
        drawBracket(g, (b.x + drift) * w, b.y * h, b.w * w, b.h * h, b.label, 0.86);
      });

      mock.raf = requestAnimationFrame(frame);
    }
    mock.raf = requestAnimationFrame(frame);

    // A plausible stats tick, so the label chrome is exercised too.
    mock.timer = setInterval(function () {
      el.camRes.textContent = "640×480";
      el.camFps.textContent = (22 + Math.round(Math.random() * 5)) + " fps";
    }, 1000);
  }

  function stopMockCamera() {
    cancelAnimationFrame(mock.raf);
    clearInterval(mock.timer);
    el.camMock.hidden = true;
    el.camFps.textContent = "— fps";
  }

  /* The same corner-bracket marks the server draws on the real feed. */
  function drawBracket(g, x, y, w, h, label, conf) {
    var arm = Math.min(w, h) * 0.28;
    g.strokeStyle = "rgba(56,232,255,0.92)";
    g.lineWidth = 2;
    g.shadowBlur = 8;
    g.shadowColor = "rgba(56,232,255,0.7)";
    var corners = [
      [x, y, 1, 1], [x + w, y, -1, 1],
      [x, y + h, 1, -1], [x + w, y + h, -1, -1]
    ];
    corners.forEach(function (c) {
      g.beginPath();
      g.moveTo(c[0] + c[2] * arm, c[1]);
      g.lineTo(c[0], c[1]);
      g.lineTo(c[0], c[1] + c[3] * arm);
      g.stroke();
    });
    g.shadowBlur = 0;
    g.fillStyle = "rgba(56,232,255,0.95)";
    g.font = "11px ui-monospace, Consolas, monospace";
    g.fillText(label + " " + Math.round(conf * 100) + "%", x + 2, Math.max(y - 5, 12));
  }

  function mockAnswer() {
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve({ transcript: "what can you see right now?", text: MOCK_ANSWER });
      }, 900);
    });
  }

  /*
   * Drives the whole page from a script when there is no backend, so the
   * orb, the chips and the caption bar can all be seen working. This is
   * review scaffolding, not the product: it is only reachable from
   * file:// or an explicit ?mock.
   */
  function runMockShow() {
    var script = [
      { state: "observing", wait: 3400 },
      { state: "thinking", wait: 2200 },
      { state: "speaking", wait: 2600, say: "There is a cup on the desk." },
      { state: "observing", wait: 4200 },
      { state: "listening", wait: 2600 },
      { state: "thinking", wait: 1800 },
      { state: "speaking", wait: 3000, say: "The light in here is warm — I like it." }
    ];
    var i = 0;

    function step() {
      if (recording || busy) {
        mock.phase = setTimeout(step, 800);
        return;
      }
      var s = script[i % script.length];
      i++;
      setState(s.state);
      if (s.state === "listening") {
        var amp = setInterval(function () { orb.setAmplitude(Math.random() * 0.7); }, 90);
        setTimeout(function () { clearInterval(amp); orb.setAmplitude(0); }, s.wait);
      }
      if (s.say) {
        setAura(s.say, false);
        var p = setInterval(function () { orb.pulse(); }, 320);
        setTimeout(function () { clearInterval(p); }, s.wait);
      }
      mock.phase = setTimeout(step, s.wait);
    }
    step();
  }

  // ----------------------------------------------------------------- live

  function connect() {
    var es = new EventSource("/events");

    es.onopen = function () {
      setLink("live", "link · live");
    };

    es.onerror = function () {
      setLink("down", "link · reconnecting");
      // EventSource reconnects on its own; no retry logic here.
    };

    es.addEventListener("state", function (e) {
      var msg = JSON.parse(e.data);
      if (!recording && !busy) setState(msg.state);
    });

    es.addEventListener("narration", function (e) {
      var msg = JSON.parse(e.data);

      // Suppressed lines are shown but never spoken: that is the
      // drop-don't-queue policy, made visible. Showing them matters —
      // otherwise "AURA chose to stay quiet" and "AURA noticed nothing"
      // look identical from here.
      setAmbient(msg.text, msg.suppressed);
      if (msg.suppressed) return;

      setState("speaking");
      speak(msg.text).then(function () {
        if (!recording && !busy) setState("observing");
      });
    });

    es.addEventListener("stats", function (e) {
      var msg = JSON.parse(e.data);
      if (msg.width) el.camRes.textContent = msg.width + "×" + msg.height;
      if (msg.fps) el.camFps.textContent = Math.round(msg.fps) + " fps";
      if (msg.body_id) document.getElementById("cam-id").textContent = msg.body_id;
    });

    es.addEventListener("error", function (e) {
      var msg = JSON.parse(e.data);
      toast(msg.message, true);
    });
  }

  // --------------------------------------------------------------- wiring

  el.orb.addEventListener("click", talk);
  el.btnTalk.addEventListener("click", function (e) {
    e.stopPropagation();
    talk();
  });
  el.btnCam.addEventListener("click", function (e) {
    e.stopPropagation();
    toggleCamera();
  });

  window.addEventListener("keydown", function (e) {
    if (e.repeat) return;
    var tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;

    if (e.code === "Space" || e.code === "Enter" || e.key === "Enter") {
      e.preventDefault();
      talk();
    } else if (e.key === "c" || e.key === "C") {
      e.preventDefault();
      toggleCamera();
    } else if (e.key === "Escape" && cameraOpen) {
      setCamera(false);
    }
  });

  // A held Space would otherwise fire the browser's key-repeat into a
  // stream of talk/stop toggles. Swallow it — the `e.repeat` check above
  // already covers keydown, this covers the browser default.
  window.addEventListener("keyup", function (e) {
    if (e.code === "Space") e.preventDefault();
  });

  window.addEventListener("beforeunload", function () {
    if (window.speechSynthesis) window.speechSynthesis.cancel();

    // If the tab goes away mid-exchange, release the server's hold so
    // narration resumes for anyone else looking. sendBeacon is the only
    // request a browser reliably completes during unload; a normal fetch
    // here is cancelled. The server also expires a stale exchange after
    // two minutes, so this is the fast path, not the safety net.
    if (!MOCK && (recording || busy) && navigator.sendBeacon) {
      navigator.sendBeacon("/exchange/end");
    }
  });

  // A dropped MJPEG connection leaves a broken-image icon and no
  // explanation. Say so instead.
  el.camFeed.addEventListener("error", function () {
    if (!cameraOpen) return;
    el.camEmpty.textContent = "camera feed unavailable";
    el.camEmpty.hidden = false;
  });

  if (MOCK) {
    setLink("down", "link · preview");
    el.orbHint.innerHTML =
      "preview mode — no backend. click <kbd>c</kbd> for the camera panel";
    runMockShow();
  } else {
    setLink("down", "link · connecting");
    connect();
  }
})();
