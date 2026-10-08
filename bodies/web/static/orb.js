/*
 * orb.js — AURA's face: a reactor core drawn on a 2-D canvas.
 *
 * No dependency, no library. A face is a few dozen primitives moving on
 * real signals; canvas 2-D is the right tool and three.js would be a much
 * larger thing to maintain for the same result.
 *
 * The important design rule: **every visual is driven by a real signal,
 * never by a timer.** `setState()` follows what AURA is actually doing
 * (the Body publishes it), and `setAmplitude()` follows the live
 * microphone while she listens. An orb that waves on its own clock would
 * look identical whether or not anything is happening, which is exactly
 * the failure this face exists to avoid — the user must be able to tell
 * from across the room whether AURA heard them.
 *
 * States are *targets*, not pictures. The orb eases toward the current
 * state's values every frame, so a change reads as the core reacting
 * rather than snapping between two images.
 */
(function (global) {
  "use strict";

  var TAU = Math.PI * 2;

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function rgba(rgb, a) {
    return "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + a + ")";
  }

  // Per-state targets. `ring` is ring angular speed, `glow` the halo
  // strength, `eye` vertical eye openness, `core` core radius scale.
  var PRESETS = {
    observing: { rgb: [56, 232, 255], ring: 0.30, glow: 0.55, eye: 0.72, core: 1.0 },
    listening: { rgb: [95, 244, 255], ring: 1.1, glow: 0.95, eye: 1.0, core: 1.08 },
    thinking: { rgb: [255, 176, 32], ring: 2.3, glow: 0.85, eye: 0.34, core: 0.94 },
    speaking: { rgb: [167, 139, 250], ring: 1.45, glow: 0.9, eye: 0.8, core: 1.05 }
  };

  // Three orbits at different tilts and squash factors. The tilt is what
  // sells "three-dimensional" — without it the rings are concentric
  // circles and the core looks flat.
  var RINGS = [
    { r: 0.34, tilt: -0.35, squash: 0.34, dir: 1, arc: 0.22 },
    { r: 0.43, tilt: 0.9, squash: 0.26, dir: -1, arc: 0.16 },
    { r: 0.5, tilt: 2.1, squash: 0.44, dir: 0.6, arc: 0.1 }
  ];

  var MOTES = 26;

  function Orb(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");

    var preset = PRESETS.observing;
    this.state = "observing";
    this.target = Object.assign({}, preset);
    this.cur = Object.assign({}, preset);

    this.phase = 0;
    this.amplitude = 0; // live mic level, 0..1
    this.ampTarget = 0;

    this.blink = 0; // 1 = fully open, 0 = closed
    this.blinkWait = 3 + Math.random() * 3;

    this.waves = []; // expanding rings emitted on each spoken phrase
    this.motes = [];
    for (var i = 0; i < MOTES; i++) {
      this.motes.push({
        a: Math.random() * TAU,
        r: 0.36 + Math.random() * 0.16,
        v: (0.15 + Math.random() * 0.4) * (Math.random() < 0.5 ? -1 : 1),
        s: 0.6 + Math.random() * 1.4
      });
    }

    this._last = 0;
    this._raf = 0;
    this._onResize = this._resize.bind(this);
    global.addEventListener("resize", this._onResize);
    this._resize();
  }

  Orb.prototype._resize = function () {
    var dpr = global.devicePixelRatio || 1;
    var rect = this.canvas.getBoundingClientRect();
    var size = Math.max(rect.width, rect.height, 1);
    this.canvas.width = Math.round(size * dpr);
    this.canvas.height = Math.round(size * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.size = size;
  };

  Orb.prototype.setState = function (name) {
    var preset = PRESETS[name] || PRESETS.observing;
    this.state = name;
    this.target = Object.assign({}, preset);
    if (global.document) {
      global.document.documentElement.setAttribute("data-aura-state", name);
    }
  };

  /* Live microphone level, 0..1. Only meaningful while listening. */
  Orb.prototype.setAmplitude = function (value) {
    this.ampTarget = Math.max(0, Math.min(1, value || 0));
  };

  /* One expanding ring. Called per spoken phrase (or per word boundary). */
  Orb.prototype.pulse = function () {
    this.waves.push({ r: 0.2, a: 0.55 });
    if (this.waves.length > 6) this.waves.shift();
  };

  Orb.prototype.start = function () {
    if (this._raf) return;
    var self = this;
    this._last = performance.now();
    function frame(now) {
      var dt = Math.min((now - self._last) / 1000, 0.05);
      self._last = now;
      self._draw(dt);
      self._raf = requestAnimationFrame(frame);
    }
    this._raf = requestAnimationFrame(frame);
  };

  Orb.prototype.stop = function () {
    cancelAnimationFrame(this._raf);
    this._raf = 0;
    global.removeEventListener("resize", this._onResize);
  };

  Orb.prototype._draw = function (dt) {
    var ctx = this.ctx;
    var size = this.size;

    // Ease toward the target state. The rates differ on purpose: colour
    // and glow land quickly so a state change is visible immediately,
    // while ring speed ramps so the core looks like it is spinning up.
    var k = Math.min(dt * 3.2, 1);
    this.cur.rgb = this.cur.rgb.map(function (c, i) {
      return lerp(c, this.target.rgb[i], k);
    }, this);
    this.cur.ring = lerp(this.cur.ring, this.target.ring, Math.min(dt * 1.6, 1));
    this.cur.glow = lerp(this.cur.glow, this.target.glow, k);
    this.cur.eye = lerp(this.cur.eye, this.target.eye, Math.min(dt * 6, 1));
    this.cur.core = lerp(this.cur.core, this.target.core, k);

    this.amplitude = lerp(this.amplitude, this.ampTarget, Math.min(dt * 12, 1));
    // Decay the meter on its own: if the analyser stops feeding us, the
    // core must fall back to rest rather than freeze mid-pulse.
    this.ampTarget *= 0.86;

    this.phase += dt * this.cur.ring;

    this._updateBlink(dt);

    var rgb = this.cur.rgb.map(Math.round);
    var cx = size / 2;
    var cy = size / 2;
    var R = size / 2;

    ctx.clearRect(0, 0, size, size);

    this._drawHalo(ctx, cx, cy, R, rgb);
    this._drawWaves(ctx, cx, cy, R, rgb, dt);
    var i;
    for (i = 0; i < RINGS.length; i++) {
      this._drawRing(ctx, cx, cy, R, RINGS[i], rgb, 1);
    }
    this._drawTicks(ctx, cx, cy, R, rgb);
    this._drawMotes(ctx, cx, cy, R, rgb, dt);
    this._drawCore(ctx, cx, cy, R, rgb);
  };

  Orb.prototype._updateBlink = function (dt) {
    this.blinkWait -= dt;
    if (this.blinkWait <= 0) {
      this.blink = Math.max(0, this.blink - dt * 14);
      if (this.blink === 0) {
        this.blinkWait = 2.5 + Math.random() * 4.5;
      }
    } else if (this.blink < 1) {
      this.blink = Math.min(1, this.blink + dt * 7);
    }
  };

  Orb.prototype._drawHalo = function (ctx, cx, cy, R, rgb) {
    var strength = this.cur.glow * (0.75 + this.amplitude * 0.6);
    var g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
    g.addColorStop(0, rgba(rgb, 0.3 * strength));
    g.addColorStop(0.32, rgba(rgb, 0.12 * strength));
    g.addColorStop(0.62, rgba(rgb, 0.035 * strength));
    g.addColorStop(1, rgba(rgb, 0));
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, R * 2, R * 2);
  };

  Orb.prototype._drawWaves = function (ctx, cx, cy, R, rgb, dt) {
    ctx.save();
    ctx.lineWidth = 1.5;
    for (var i = this.waves.length - 1; i >= 0; i--) {
      var w = this.waves[i];
      w.r += dt * 0.75;
      w.a -= dt * 0.7;
      if (w.a <= 0) {
        this.waves.splice(i, 1);
        continue;
      }
      ctx.beginPath();
      ctx.arc(cx, cy, w.r * R, 0, TAU);
      ctx.strokeStyle = rgba(rgb, w.a);
      ctx.stroke();
    }
    ctx.restore();
  };

  Orb.prototype._drawRing = function (ctx, cx, cy, R, spec, rgb, alphaScale) {
    var rx = R * spec.r * (1 + this.amplitude * 0.05);
    var ry = rx * spec.squash;
    var a0 = this.phase * spec.dir;
    var bright = 0.55 + this.cur.glow * 0.45;
    var i;

    ctx.save();
    ctx.translate(cx, cy);

    ctx.beginPath();
    ctx.ellipse(0, 0, rx, ry, spec.tilt, 0, TAU);
    ctx.strokeStyle = rgba(rgb, 0.13 * alphaScale);
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.shadowBlur = 14;
    ctx.shadowColor = rgba(rgb, 0.8);

    ctx.beginPath();
    ctx.ellipse(0, 0, rx, ry, spec.tilt, a0, a0 + spec.arc * TAU);
    ctx.strokeStyle = rgba(rgb, bright * alphaScale);
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.beginPath();
    ctx.ellipse(
      0, 0, rx, ry, spec.tilt,
      a0 + Math.PI, a0 + Math.PI + spec.arc * 0.55 * TAU
    );
    ctx.strokeStyle = rgba(rgb, bright * 0.45 * alphaScale);
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.restore();
  };

  /* Small radial ticks around the outer ring — pure chrome, but it is
   * what makes the core read as an instrument rather than a gradient. */
  Orb.prototype._drawTicks = function (ctx, cx, cy, R, rgb) {
    var count = 48;
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(this.phase * 0.12);
    ctx.strokeStyle = rgba(rgb, 0.22);
    ctx.lineWidth = 1;
    for (var i = 0; i < count; i++) {
      var a = (i / count) * TAU;
      var long = i % 4 === 0;
      var r0 = R * 0.545;
      var r1 = r0 + (long ? R * 0.03 : R * 0.014);
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0);
      ctx.lineTo(Math.cos(a) * r1, Math.sin(a) * r1);
      ctx.stroke();
    }
    ctx.restore();
  };

  Orb.prototype._drawMotes = function (ctx, cx, cy, R, rgb, dt) {
    ctx.save();
    ctx.fillStyle = rgba(rgb, 0.75);
    for (var i = 0; i < this.motes.length; i++) {
      var m = this.motes[i];
      m.a += m.v * dt * (0.6 + this.cur.ring);
      var rx = R * m.r;
      var ry = rx * 0.4;
      var tilt = 0.35;
      var x = Math.cos(m.a) * rx;
      var y = Math.sin(m.a) * ry;
      ctx.beginPath();
      ctx.arc(
        cx + x * Math.cos(tilt) - y * Math.sin(tilt),
        cy + x * Math.sin(tilt) + y * Math.cos(tilt),
        m.s * 0.9,
        0,
        TAU
      );
      ctx.fill();
    }
    ctx.restore();
  };

  Orb.prototype._drawCore = function (ctx, cx, cy, R, rgb) {
    var pulse = 1 + this.amplitude * 0.16;
    var r = R * 0.19 * this.cur.core * pulse;

    ctx.save();
    ctx.shadowBlur = 30 * this.cur.glow;
    ctx.shadowColor = rgba(rgb, 0.9);

    var g = ctx.createRadialGradient(cx, cy - r * 0.3, 0, cx, cy, r);
    g.addColorStop(0, rgba([255, 255, 255], 0.95));
    g.addColorStop(0.35, rgba(rgb, 0.85));
    g.addColorStop(1, rgba(rgb, 0.18));
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, TAU);
    ctx.fill();
    ctx.restore();

    this._drawEyes(ctx, cx, cy, r, rgb);
  };

  /* Two lens-shaped eyes on the core. They open and close on the state,
   * and blink on their own, which is most of what makes the thing read
   * as an entity rather than a lamp. */
  Orb.prototype._drawEyes = function (ctx, cx, cy, r, rgb) {
    var open = this.cur.eye * this.blink;
    var w = r * 0.13;
    var h = Math.max(r * 0.26 * open, 0.5);
    var dx = r * 0.34;
    var dy = -r * 0.08;

    ctx.save();
    ctx.shadowBlur = 10;
    ctx.shadowColor = "rgba(0,40,60,0.9)";
    ctx.fillStyle = "rgba(2,10,18,0.92)";
    var sides = [-1, 1];
    for (var i = 0; i < sides.length; i++) {
      ctx.beginPath();
      ctx.ellipse(cx + sides[i] * dx, cy + dy, w, h, 0, 0, TAU);
      ctx.fill();
    }
    ctx.restore();
  };

  global.Orb = Orb;
})(window);
