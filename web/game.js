/**
 * Snake Multiplayer – Web Client
 * Connects via WebSocket to server.py and renders the game on an HTML5 Canvas.
 */

"use strict";

// ─── Constants ────────────────────────────────────────────────────────────────

const PLAYER_COLORS = [
  "#00e676", "#ff1744", "#2979ff", "#ffea00",
  "#d500f9", "#00e5ff", "#ffffff", "#76ff03",
  "#ff6d00", "#f50057",
];

const WEAPON_ICONS = { bomb: "💣", ghost: "👻", shotgun: "🔫", nuclear: "☢️" };

// Direction indices (match Python enum)
const DIR = { UP: 0, DOWN: 1, LEFT: 2, RIGHT: 3 };

// Explosion colours
const EXP_COLORS  = ["#ff6600","#ff9900","#ffcc00","#ff3300","#ffff00"];
const NUKE_COLORS = ["#00ff44","#88ff00","#ccff00","#00cc44","#ffffff"];

// ─── Global state ─────────────────────────────────────────────────────────────

let ws          = null;
let gameState   = null;
let myPlayerId  = null;
let isSpectator = false;

/** JS-side particle list for explosion effects */
const particles  = [];
let prevExplosions = [];   // to detect newly spawned explosions

/** Whether we have been welcomed (have player_id) */
let welcomed = false;

// ─── DOM elements ─────────────────────────────────────────────────────────────

const connectingOverlay = document.getElementById("connectingOverlay");
const lobbyOverlay      = document.getElementById("lobbyOverlay");
const nameSection       = document.getElementById("nameSection");
const lobbySection      = document.getElementById("lobbySection");
const nameInput         = document.getElementById("nameInput");
const joinBtn           = document.getElementById("joinBtn");
const startBtn          = document.getElementById("startBtn");
const playerList        = document.getElementById("playerList");
const spectatorBanner   = document.getElementById("spectatorBanner");
const hudStatus         = document.getElementById("hudStatus");
const hudWeapons        = document.getElementById("hudWeapons");
const hudScore          = document.getElementById("hudScore");
const rotateHint        = document.getElementById("rotateHint");
const canvas            = document.getElementById("gameCanvas");
const ctx               = canvas.getContext("2d");

// ─── Cell size & canvas sizing ────────────────────────────────────────────────

let cellSize  = 14;
let gameW     = 80;
let gameH     = 45;

function computeCellSize(gw, gh) {
  const hudH    = 42;
  const ctrlH   = isTouchDevice() ? 130 : 0;
  const spectH  = isSpectator && !spectatorBanner.classList.contains("hidden") ? 28 : 0;
  const maxW = window.innerWidth  - 4;
  const maxH = window.innerHeight - hudH - ctrlH - spectH - 8;
  return Math.max(4, Math.min(Math.floor(maxW / gw), Math.floor(maxH / gh)));
}

function resizeCanvas() {
  if (!gameState) return;
  gameW = gameState.width  || 80;
  gameH = gameState.height || 45;
  cellSize = computeCellSize(gameW, gameH);
  canvas.width  = gameW * cellSize;
  canvas.height = gameH * cellSize;
}

window.addEventListener("resize", () => { resizeCanvas(); });

// ─── WebSocket ────────────────────────────────────────────────────────────────

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url   = `${proto}://${location.host}/ws`;
  ws = new WebSocket(url);

  ws.addEventListener("open",    onOpen);
  ws.addEventListener("message", onMessage);
  ws.addEventListener("close",   onClose);
  ws.addEventListener("error",   onError);
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function onOpen() {
  // waiting for first state message from server
}

function onClose() {
  welcomed    = false;
  myPlayerId  = null;
  gameState   = null;
  showOverlay("connecting");
  setTimeout(connect, 2000);
}

function onError(e) { console.error("WS error", e); }

function onMessage(evt) {
  let msg;
  try { msg = JSON.parse(evt.data); } catch { return; }

  if (msg.type === "welcome") {
    myPlayerId  = msg.player_id;
    isSpectator = msg.spectator;
    welcomed    = true;

    if (isSpectator) {
      showOverlay(null);
      spectatorBanner.classList.remove("hidden");
    } else {
      // show lobby section (name already set)
      nameSection.classList.add("hidden");
      lobbySection.classList.remove("hidden");
      showOverlay("lobby");
    }

  } else if (msg.type === "state") {
    const prev = gameState;
    gameState  = msg.game;

    resizeCanvasIfNeeded(prev);
    checkNewExplosions();
    updateUI();

    // Close lobby when game starts
    if (welcomed && !isSpectator &&
        (gameState.state === "running" || gameState.state === "countdown")) {
      if (!lobbyOverlay.classList.contains("hidden")) {
        showOverlay(null);
      }
    }

    // First state received (not yet welcomed) → show name form
    if (!welcomed) {
      if (gameState.state === "waiting") {
        showOverlay("lobby");
      } else {
        // Game is already running; will auto-spectate after joining
        showOverlay("lobby");
      }
    }
  }
}

let lastDims = "";
function resizeCanvasIfNeeded(prev) {
  const key = `${gameState.width}x${gameState.height}`;
  if (key !== lastDims) {
    lastDims = key;
    resizeCanvas();
  }
}

// ─── UI helpers ───────────────────────────────────────────────────────────────

function showOverlay(which) {
  connectingOverlay.classList.add("hidden");
  lobbyOverlay.classList.add("hidden");

  if (which === "connecting") connectingOverlay.classList.remove("hidden");
  else if (which === "lobby") lobbyOverlay.classList.remove("hidden");
}

function isTouchDevice() {
  return navigator.maxTouchPoints > 0;
}

function updateUI() {
  if (!gameState) return;

  // Lobby player list
  if (!lobbyOverlay.classList.contains("hidden") && gameState.snakes) {
    renderPlayerList();
  }

  // Sync settings buttons to server state
  syncSettingsUI();

  // Spectator banner
  if (isSpectator) {
    spectatorBanner.classList.remove("hidden");
  }

  // HUD
  updateHUD();
}

function renderPlayerList() {
  const snakes = Object.values(gameState.snakes || {});
  if (snakes.length === 0) {
    playerList.innerHTML = `<div class="empty-list">Waiting for players…</div>`;
    return;
  }
  playerList.innerHTML = snakes.map((s, i) => {
    const color = PLAYER_COLORS[s.player_id % PLAYER_COLORS.length] || "#fff";
    return `<div class="player-entry">
      <div class="player-dot" style="background:${color}"></div>
      <span>${escHtml(s.player_name)}</span>
    </div>`;
  }).join("");
}

function escHtml(s) {
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function updateHUD() {
  if (!gameState) return;

  // State label
  const stateLabel = {
    waiting:   "⏳ LOBBY",
    countdown: `⏱ ${gameState.countdown || ""}`,
    running:   "▶ RUNNING",
    finished:  "🏁 FINISHED",
  }[gameState.state] || gameState.state;

  const walls = gameState.walls_enabled ? "walls" : "wrap";
  hudStatus.textContent = `${stateLabel}  ·  ${gameState.mode}  ·  ${gameState.speed}  ·  ${walls}`;

  // Weapon queue for my snake
  const mySnake = gameState.snakes?.[myPlayerId];
  if (mySnake && mySnake.weapon_queue && mySnake.weapon_queue.length > 0) {
    const icons = mySnake.weapon_queue.map(w => WEAPON_ICONS[w] || w).join(" › ");
    hudWeapons.textContent = `SPACE: ${icons}`;
  } else {
    hudWeapons.textContent = "";
  }

  // Score
  if (mySnake) {
    const alive = mySnake.alive ? "🟢" : "💀";
    hudScore.textContent = `${alive} ${mySnake.score ?? 0} pts`;
  } else {
    hudScore.textContent = "";
  }
}

// ─── Particle system ──────────────────────────────────────────────────────────

function spawnExplosionParticles(gx, gy, isNuclear) {
  const cx    = (gx + 0.5) * cellSize;
  const cy    = (gy + 0.5) * cellSize;
  const count = isNuclear ? 80 : 24;
  const speed = isNuclear ? 14 : 6;
  const colors = isNuclear ? NUKE_COLORS : EXP_COLORS;

  for (let i = 0; i < count; i++) {
    const angle = Math.random() * Math.PI * 2;
    const v     = (Math.random() * speed) + 2;
    particles.push({
      x: cx, y: cy,
      vx: Math.cos(angle) * v,
      vy: Math.sin(angle) * v - Math.random() * 3,
      life: Math.random() * 35 + 25,
      maxLife: 60,
      color: colors[Math.floor(Math.random() * colors.length)],
      size: Math.random() * (isNuclear ? 5 : 3) + 1,
    });
  }
}

function checkNewExplosions() {
  if (!gameState) return;
  const cur = gameState.explosions || [];
  for (const exp of cur) {
    const wasHere = prevExplosions.some(p => p.x === exp.x && p.y === exp.y);
    if (!wasHere) {
      spawnExplosionParticles(exp.x, exp.y, exp.is_nuclear);
    }
  }
  prevExplosions = cur.slice();
}

function updateParticles() {
  for (let i = particles.length - 1; i >= 0; i--) {
    const p = particles[i];
    p.x  += p.vx;
    p.y  += p.vy;
    p.vy += 0.25;
    p.life--;
    if (p.life <= 0) { particles.splice(i, 1); continue; }
    const a = p.life / p.maxLife;
    ctx.globalAlpha = a;
    ctx.fillStyle   = p.color;
    const s = p.size * a + 0.5;
    ctx.fillRect(p.x - s/2, p.y - s/2, s, s);
  }
  ctx.globalAlpha = 1;
}

// ─── Rendering ────────────────────────────────────────────────────────────────

function render() {
  requestAnimationFrame(render);
  if (!gameState) return;

  const g  = gameState;
  const cs = cellSize;

  // Clear
  ctx.fillStyle = "#0d0d1a";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // ── Walls
  drawWalls(g, cs);

  // ── Food
  for (const f of (g.foods || [])) {
    drawFood(f[0], f[1], cs);
  }

  // ── Pickups
  for (const w of (g.weapons         || [])) drawPickup(w[0], w[1], cs, "bomb");
  for (const w of (g.ghost_pickups   || [])) drawPickup(w[0], w[1], cs, "ghost");
  for (const w of (g.shotgun_pickups || [])) drawPickup(w[0], w[1], cs, "shotgun");
  for (const w of (g.nuclear_pickups || [])) drawPickup(w[0], w[1], cs, "nuclear");

  // ── Snakes
  for (const [pid, snake] of Object.entries(g.snakes || {})) {
    if (!snake.body || snake.body.length === 0) continue;
    drawSnake(snake, pid === myPlayerId, cs);
  }

  // ── Bombs / bullets in flight
  for (const bomb of (g.bombs || [])) {
    drawBomb(bomb, cs);
  }

  // ── Explosions
  for (const exp of (g.explosions || [])) {
    drawExplosion(exp, cs);
  }

  // ── Particles
  updateParticles();

  // ── Countdown overlay
  if (g.state === "countdown" && g.countdown > 0) {
    drawCountdown(g.countdown, cs);
  }

  // ── Winner overlay
  if (g.state === "finished") {
    drawWinnerScreen(g, cs);
  }

  // ── Waiting: player names on canvas
  if (g.state === "waiting") {
    drawWaitingNames(g, cs);
  }
}

// -- Walls

function drawWalls(g, cs) {
  if (!g.walls_enabled) return;

  ctx.fillStyle   = "#424242";
  ctx.strokeStyle = "#2a2a2a";
  ctx.lineWidth   = 0.5;

  const destroyed = new Set((g.destroyed_wall_segments || []).map(s => `${s[0]},${s[1]}`));

  if (g.shrinking_walls_active && g.shrinking_wall_bounds) {
    const b = g.shrinking_wall_bounds;
    // top row
    for (let x = b.left; x <= b.right; x++) drawWallCell(x, b.top, cs, destroyed);
    // bottom row
    for (let x = b.left; x <= b.right; x++) drawWallCell(x, b.bottom, cs, destroyed);
    // left col
    for (let y = b.top + 1; y < b.bottom; y++) drawWallCell(b.left, y, cs, destroyed);
    // right col
    for (let y = b.top + 1; y < b.bottom; y++) drawWallCell(b.right, y, cs, destroyed);
  } else {
    const W = g.width, H = g.height;
    for (let x = 0; x < W; x++) {
      drawWallCell(x, 0,     cs, destroyed);
      drawWallCell(x, H - 1, cs, destroyed);
    }
    for (let y = 1; y < H - 1; y++) {
      drawWallCell(0,     y, cs, destroyed);
      drawWallCell(W - 1, y, cs, destroyed);
    }
  }
}

function drawWallCell(x, y, cs, destroyed) {
  if (destroyed.has(`${x},${y}`)) return;
  ctx.fillRect(x * cs, y * cs, cs, cs);
  ctx.strokeRect(x * cs, y * cs, cs, cs);
}

// -- Food

function drawFood(x, y, cs) {
  const cx = x * cs + cs / 2;
  const cy = y * cs + cs / 2;
  const r  = cs / 2 - 1;

  ctx.fillStyle = "#ff3232";
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.fill();

  // shine
  ctx.fillStyle = "rgba(255,255,255,.35)";
  ctx.beginPath();
  ctx.arc(cx - r * .3, cy - r * .3, r * .3, 0, Math.PI * 2);
  ctx.fill();
}

// -- Pickups

function drawPickup(x, y, cs, type) {
  const cx = (x + .5) * cs;
  const cy = (y + .5) * cs;
  const r  = cs * .42;

  if (type === "bomb") {
    // Body
    ctx.fillStyle = "#333";
    ctx.beginPath();
    ctx.arc(cx, cy + r * .15, r, 0, Math.PI * 2);
    ctx.fill();
    // Fuse (wavy line)
    ctx.strokeStyle = "#ffcc00";
    ctx.lineWidth   = Math.max(1, cs * .1);
    ctx.beginPath();
    ctx.moveTo(cx, cy - r * .85);
    ctx.quadraticCurveTo(cx + r * .4, cy - r * 1.3, cx + r * .1, cy - r * .8);
    ctx.stroke();
    // Spark
    ctx.fillStyle = "#ff9900";
    ctx.beginPath();
    ctx.arc(cx + r * .1, cy - r * .8, cs * .08, 0, Math.PI * 2);
    ctx.fill();

  } else if (type === "ghost") {
    ctx.fillStyle = "rgba(100,180,255,.85)";
    ctx.beginPath();
    ctx.arc(cx, cy - r * .2, r, Math.PI, 0);
    ctx.lineTo(cx + r, cy + r * .6);
    // scalloped bottom
    for (let i = 3; i >= 0; i--) {
      const sx = cx + r - (i + .5) * (r * 2 / 4);
      ctx.quadraticCurveTo(sx + r / 8, cy + r * .85, sx - r / 8, cy + r * .6);
    }
    ctx.closePath();
    ctx.fill();
    // Eyes
    ctx.fillStyle = "#0d0d1a";
    ctx.beginPath();
    ctx.arc(cx - r * .3, cy - r * .2, r * .18, 0, Math.PI * 2);
    ctx.arc(cx + r * .3, cy - r * .2, r * .18, 0, Math.PI * 2);
    ctx.fill();

  } else if (type === "shotgun") {
    ctx.fillStyle = "#e65100";
    const w = cs * .85, h = cs * .3;
    ctx.fillRect(cx - w/2, cy - h/2, w, h);
    // barrel
    ctx.fillStyle = "#bf360c";
    ctx.fillRect(cx + w * .2, cy - h * .2, w * .35, h * .4);
    // handle
    ctx.fillStyle = "#4e2600";
    ctx.fillRect(cx - w * .2, cy, w * .25, h * .8);

  } else if (type === "nuclear") {
    // 2×2 nuclear pickup – draw glowing circle
    const grd = ctx.createRadialGradient(cx, cy, 0, cx, cy, r * 1.6);
    grd.addColorStop(0,   "rgba(0,255,100,.9)");
    grd.addColorStop(.6,  "rgba(0,200,50,.4)");
    grd.addColorStop(1,   "rgba(0,200,50,0)");
    ctx.fillStyle = grd;
    ctx.beginPath();
    ctx.arc(cx, cy, r * 1.6, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#00ff64";
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();

    // ☢ symbol approx – 3 arcs
    ctx.fillStyle = "#0d0d1a";
    ctx.font = `bold ${Math.max(8, cs * .7)}px serif`;
    ctx.textAlign    = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("☢", cx, cy);
  }
}

// -- Snakes

function drawSnake(snake, isMe, cs) {
  const color = PLAYER_COLORS[snake.player_id % PLAYER_COLORS.length] || "#fff";
  const body  = snake.body;
  if (!body || body.length === 0) return;

  const invisible = snake.is_invisible;
  const alpha = invisible ? (isMe ? 0.35 : 0) : (snake.alive ? 1 : 0.4);
  if (alpha === 0) return;

  ctx.globalAlpha = alpha;

  // Body segments (draw tail → head-1)
  const bodyColor = adjustBrightness(color, -.25);
  ctx.fillStyle   = bodyColor;
  for (let i = body.length - 1; i >= 1; i--) {
    const [bx, by] = body[i];
    const pad = Math.max(1, cs * .1);
    roundRect(bx * cs + pad, by * cs + pad, cs - pad*2, cs - pad*2, Math.max(2, cs*.2));
  }

  // Head
  const [hx, hy] = body[0];
  drawHead(hx, hy, color, snake.direction, snake.alive, cs);

  // Name tag above head (while alive)
  if (snake.alive && cs >= 8) {
    ctx.fillStyle = "rgba(0,0,0,.6)";
    const label = snake.player_name;
    ctx.font = `${Math.max(8, cs * .7)}px Courier New`;
    const tw  = ctx.measureText(label).width;
    const tx  = hx * cs + cs / 2 - tw / 2;
    const ty  = hy * cs - 3;
    ctx.fillRect(tx - 2, ty - ctx.measureText("M").width * .9 - 2, tw + 4, ctx.measureText("M").width * .9 + 4);
    ctx.fillStyle = color;
    ctx.textAlign    = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText(label, tx, ty);
  }

  ctx.globalAlpha = 1;
}

function drawHead(x, y, color, dir, alive, cs) {
  const cx = (x + .5) * cs;
  const cy = (y + .5) * cs;
  const r  = cs * .48;

  ctx.save();
  ctx.translate(cx, cy);

  // Rotate so face points in direction of travel
  const rot = [Math.PI, 0, -Math.PI/2, Math.PI/2];
  ctx.rotate(rot[dir] ?? 0);

  // Head circle
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.fill();

  if (alive) {
    // White eyes
    ctx.fillStyle = "white";
    ctx.beginPath();
    ctx.arc(-r * .38, -r * .35, r * .22, 0, Math.PI * 2);
    ctx.arc( r * .38, -r * .35, r * .22, 0, Math.PI * 2);
    ctx.fill();

    // Pupils
    ctx.fillStyle = "#111";
    ctx.beginPath();
    ctx.arc(-r * .38, -r * .28, r * .11, 0, Math.PI * 2);
    ctx.arc( r * .38, -r * .28, r * .11, 0, Math.PI * 2);
    ctx.fill();
  } else {
    // X eyes for dead snake
    ctx.strokeStyle = "#111";
    ctx.lineWidth   = Math.max(1, r * .15);
    [-1, 1].forEach(side => {
      const ex = side * r * .38;
      const ey = -r * .32;
      const d  = r * .16;
      ctx.beginPath();
      ctx.moveTo(ex - d, ey - d); ctx.lineTo(ex + d, ey + d);
      ctx.moveTo(ex + d, ey - d); ctx.lineTo(ex - d, ey + d);
      ctx.stroke();
    });
  }

  ctx.restore();
}

// -- Bombs in flight

function drawBomb(bomb, cs) {
  const type = bomb.weapon_type || "bomb";

  if (type === "shotgun") {
    // Small fast bullet
    ctx.fillStyle = "#ff9900";
    const s = Math.max(2, cs * .25);
    ctx.fillRect(bomb.x * cs + (cs - s)/2, bomb.y * cs + (cs - s)/2, s, s);

  } else if (type === "nuclear") {
    // 2×2 glowing green ball
    const grd = ctx.createRadialGradient(
      (bomb.x + 1) * cs, (bomb.y + 1) * cs, 0,
      (bomb.x + 1) * cs, (bomb.y + 1) * cs, cs * 1.2,
    );
    grd.addColorStop(0,  "#00ff64");
    grd.addColorStop(.5, "#00bb44");
    grd.addColorStop(1,  "rgba(0,200,50,0)");
    ctx.fillStyle = grd;
    ctx.beginPath();
    ctx.arc((bomb.x + 1) * cs, (bomb.y + 1) * cs, cs * 1.2, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#00ff64";
    ctx.beginPath();
    ctx.arc((bomb.x + 1) * cs, (bomb.y + 1) * cs, cs * .6, 0, Math.PI * 2);
    ctx.fill();

  } else {
    // Normal bomb
    const cx = (bomb.x + .5) * cs;
    const cy = (bomb.y + .5) * cs;
    const r  = cs * .38;
    ctx.fillStyle = "#222";
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();

    ctx.strokeStyle = "#ff6600";
    ctx.lineWidth   = Math.max(1, cs * .08);
    ctx.beginPath();
    ctx.arc(cx, cy, r + cs * .12, -Math.PI * .6, Math.PI * .6);
    ctx.stroke();
  }
}

// -- Explosions

function drawExplosion(exp, cs) {
  const isNuclear = exp.is_nuclear;
  const ttl       = exp.ttl || 1;
  const maxTtl    = isNuclear ? 10 : 5;
  const progress  = ttl / maxTtl;
  const alpha     = Math.min(1, progress * 2);
  const radius    = isNuclear
    ? (cs * 5 + (1 - progress) * cs * 3)
    : (cs * 1.5 + (1 - progress) * cs);

  const cx = (exp.x + .5) * cs;
  const cy = (exp.y + .5) * cs;

  const inner = isNuclear ? "rgba(180,255,100," : "rgba(255,200,30,";
  const outer = isNuclear ? "rgba(0,200,50,"    : "rgba(255,60,0,";

  const grd = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
  grd.addColorStop(0,   inner + alpha + ")");
  grd.addColorStop(.5,  outer + (alpha * .7) + ")");
  grd.addColorStop(1,   outer + "0)");

  ctx.fillStyle = grd;
  ctx.beginPath();
  ctx.arc(cx, cy, radius, 0, Math.PI * 2);
  ctx.fill();
}

// -- Countdown

function drawCountdown(n, cs) {
  const fontSize = Math.min(120, cs * 7);
  ctx.font          = `bold ${fontSize}px Courier New`;
  ctx.textAlign     = "center";
  ctx.textBaseline  = "middle";
  ctx.fillStyle     = "rgba(0,0,0,.55)";
  ctx.fillText(n, canvas.width / 2 + 3, canvas.height / 2 + 3);
  ctx.fillStyle     = "#ffea00";
  ctx.fillText(n, canvas.width / 2, canvas.height / 2);
}

// -- Winner screen

function drawWinnerScreen(g, cs) {
  ctx.fillStyle = "rgba(0,0,0,.72)";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const cx = canvas.width  / 2;
  const cy = canvas.height / 2;

  const title = g.winner ? `🏆 ${g.winner} wins!` : "Game Over";
  const fs1   = Math.min(54, cs * 4);

  ctx.font          = `bold ${fs1}px Courier New`;
  ctx.textAlign     = "center";
  ctx.textBaseline  = "alphabetic";
  ctx.fillStyle     = "#ffd700";
  ctx.fillText(title, cx, cy - fs1);

  // Rankings
  const medals = ["🥇","🥈","🥉"];
  const fs2    = Math.min(26, cs * 2);
  ctx.font      = `${fs2}px Courier New`;
  (g.player_rankings || []).slice(0, 5).forEach((r, i) => {
    const medal = medals[i] || `#${i+1}`;
    ctx.fillStyle = i === 0 ? "#ffd700" : i === 1 ? "#c0c0c0" : i === 2 ? "#cd7f32" : "#aaa";
    ctx.fillText(
      `${medal}  ${r.player_name}  –  ${r.score} pts`,
      cx,
      cy + i * (fs2 + 8),
    );
  });

  // Restart hint
  const fs3 = Math.min(18, cs * 1.4);
  ctx.font      = `${fs3}px Courier New`;
  ctx.fillStyle = "#888";
  ctx.fillText("Press R to play again", cx, cy + 5 * (fs2 + 8) + fs3);
}

// -- Waiting: draw player starts

function drawWaitingNames(g, cs) {
  ctx.font          = `bold ${Math.max(8, cs * .8)}px Courier New`;
  ctx.textAlign     = "center";
  ctx.textBaseline  = "bottom";

  for (const [pid, snake] of Object.entries(g.snakes || {})) {
    if (!snake.body || snake.body.length === 0) continue;
    const [hx, hy] = snake.body[0];
    const color    = PLAYER_COLORS[snake.player_id % PLAYER_COLORS.length];
    ctx.fillStyle  = color;
    ctx.fillText(snake.player_name, (hx + .5) * cs, hy * cs - 2);
  }
}

// ─── Canvas helpers ───────────────────────────────────────────────────────────

function roundRect(x, y, w, h, r) {
  if (w < 2*r) r = w/2;
  if (h < 2*r) r = h/2;
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y,     x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x,     y + h, r);
  ctx.arcTo(x,     y + h, x,     y,     r);
  ctx.arcTo(x,     y,     x + w, y,     r);
  ctx.closePath();
  ctx.fill();
}

function adjustBrightness(hex, factor) {
  const r = parseInt(hex.slice(1,3), 16);
  const g = parseInt(hex.slice(3,5), 16);
  const b = parseInt(hex.slice(5,7), 16);
  const clamp = v => Math.max(0, Math.min(255, Math.round(v + 255 * factor)));
  return `rgb(${clamp(r)},${clamp(g)},${clamp(b)})`;
}

// ─── Keyboard input ───────────────────────────────────────────────────────────

document.addEventListener("keydown", e => {
  // Prevent arrow scrolling
  if (["ArrowUp","ArrowDown","ArrowLeft","ArrowRight"," "].includes(e.key)) {
    e.preventDefault();
  }

  if (!gameState) return;

  const state = gameState.state;

  // Join / start during lobby
  if ((state === "waiting" || !welcomed) && e.key === "Enter") {
    if (!welcomed) tryJoin();
    return;
  }
  if (state === "waiting" && (e.key === "s" || e.key === "S")) {
    send({ type: "start" });
    return;
  }
  if (state === "finished" && (e.key === "r" || e.key === "R")) {
    send({ type: "restart" });
    return;
  }

  if (isSpectator) return;

  const map = {
    ArrowUp:    "UP",
    ArrowDown:  "DOWN",
    ArrowLeft:  "LEFT",
    ArrowRight: "RIGHT",
    w: "UP", a: "LEFT", s: "DOWN", d: "RIGHT",
    W: "UP", A: "LEFT", S: "DOWN", D: "RIGHT",
    " ": "FIRE",
  };
  const action = map[e.key];
  if (action) send({ type: "input", action });
});

// ─── Touch / mobile input ─────────────────────────────────────────────────────

// Swipe on canvas
let touchX0 = 0, touchY0 = 0;
canvas.addEventListener("touchstart", e => {
  touchX0 = e.touches[0].clientX;
  touchY0 = e.touches[0].clientY;
  e.preventDefault();
}, { passive: false });

canvas.addEventListener("touchend", e => {
  if (isSpectator) return;
  const dx = e.changedTouches[0].clientX - touchX0;
  const dy = e.changedTouches[0].clientY - touchY0;
  const minSwipe = 30;

  if (Math.max(Math.abs(dx), Math.abs(dy)) < minSwipe) {
    // Tap = fire
    send({ type: "input", action: "FIRE" });
  } else if (Math.abs(dx) > Math.abs(dy)) {
    send({ type: "input", action: dx > 0 ? "RIGHT" : "LEFT" });
  } else {
    send({ type: "input", action: dy > 0 ? "DOWN" : "UP" });
  }
  e.preventDefault();
}, { passive: false });

// D-pad buttons
document.querySelectorAll(".dpad-btn").forEach(btn => {
  const action = btn.dataset.action;
  const fire   = () => { if (!isSpectator) send({ type: "input", action }); };
  btn.addEventListener("touchstart", e => { fire(); e.preventDefault(); }, { passive: false });
  btn.addEventListener("mousedown",  fire);
});

document.getElementById("fireBtn").addEventListener("touchstart", e => {
  if (!isSpectator) send({ type: "input", action: "FIRE" });
  e.preventDefault();
}, { passive: false });
document.getElementById("fireBtn").addEventListener("mousedown", () => {
  if (!isSpectator) send({ type: "input", action: "FIRE" });
});

// ─── Lobby button handlers ────────────────────────────────────────────────────

function tryJoin() {
  const name = nameInput.value.trim() || "Player";
  send({ type: "join", name });
}

joinBtn.addEventListener("click", tryJoin);

nameInput.addEventListener("keydown", e => {
  if (e.key === "Enter") tryJoin();
});

startBtn.addEventListener("click", () => send({ type: "start" }));

// ─── Settings panel ───────────────────────────────────────────────────────────

/**
 * Sync the active state of setting buttons to the current game state.
 * Called whenever a new state arrives from the server.
 */
function syncSettingsUI() {
  if (!gameState) return;
  setActiveBtn("settingMode",  gameState.mode);
  setActiveBtn("settingSpeed", gameState.speed);
  setActiveBtn("settingWalls", String(gameState.walls_enabled));
}

function setActiveBtn(groupId, value) {
  const group = document.getElementById(groupId);
  if (!group) return;
  group.querySelectorAll(".sbtn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.value === String(value));
  });
}

// Wire each settings button to send a settings message
document.querySelectorAll("#settingsPanel .sbtn").forEach(btn => {
  btn.addEventListener("click", () => {
    if (isSpectator) return;
    if (!gameState || gameState.state !== "waiting") return;

    const groupId = btn.closest(".settings-btns").id;
    const value   = btn.dataset.value;

    const msg = { type: "settings" };
    if      (groupId === "settingMode")  msg.mode  = value;
    else if (groupId === "settingSpeed") msg.speed = value;
    else if (groupId === "settingWalls") msg.walls = value === "true";

    send(msg);

    // Optimistic UI: mark this button active immediately
    setActiveBtn(groupId, value);
  });
});

// ─── Portrait detection ───────────────────────────────────────────────────────

function checkOrientation() {
  const portrait = window.innerWidth < window.innerHeight && window.innerWidth < 600;
  rotateHint.classList.toggle("hidden", !portrait);
}
window.addEventListener("orientationchange", checkOrientation);
window.addEventListener("resize",            checkOrientation);
checkOrientation();

// ─── Boot ─────────────────────────────────────────────────────────────────────

showOverlay("connecting");
connect();
requestAnimationFrame(render);
