#!/usr/bin/env python3
"""
Snake Multiplayer - Web Server
Serves the HTML/JS frontend and handles WebSocket game connections.

Usage:
    pip install aiohttp
    python server.py [--host 0.0.0.0] [--port 8080] [--mode classic|kurve]
                     [--speed normal|fast|ultra] [--no-walls]

Then open http://localhost:8080 in your browser.
"""

import asyncio
import json
import sys
import os
import time
import random
import copy
import logging
import argparse
import secrets
from dataclasses import asdict
from typing import Dict, Optional

# Add parent directory to path so we can import snake_game
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from aiohttp import web
    import aiohttp
except ImportError:
    print("ERROR: aiohttp is not installed.")
    print("Install it with:  pip install aiohttp")
    sys.exit(1)

try:
    from snake_game import (
        SnakeGameLogic, GameData, GameState, Direction,
        SPEED_SETTINGS, WEAPON_SPAWN_MIN, WEAPON_SPAWN_MAX,
    )
except ImportError as e:
    print(f"ERROR: Could not import snake_game: {e}")
    print("Make sure snake_game.py is in the parent directory.")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("snake-web")

MAX_PLAYERS = 10
DEFAULT_WIDTH  = 80
DEFAULT_HEIGHT = 45

# Integer → canonical-string maps for settings received from clients.
# Clients MUST send an integer index; the server generates the string value
# internally, so no user-supplied string ever reaches the game engine.
_MODE_MAP   = {0: "classic", 1: "kurve"}
_SPEED_MAP  = {0: "normal",  1: "fast", 2: "ultra"}
_WEAPON_MAP = {0: "bomb", 1: "ghost", 2: "shotgun", 3: "nuclear"}
_ALL_WEAPONS: frozenset = frozenset(_WEAPON_MAP.values())


# ---------------------------------------------------------------------------
# Client record
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, ws: web.WebSocketResponse, player_id: str):
        self.ws              = ws
        self.player_id       = player_id
        self.name            = ""
        self.joined          = False   # True while an active player in game.snakes
        self.spectator       = False   # True while only watching
        self.ip: str         = ""
        self.msg_timestamps: list = []

    async def send(self, msg: dict):
        try:
            await self.ws.send_str(json.dumps(msg))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Game server
# ---------------------------------------------------------------------------

class WebSnakeServer:
    def __init__(self, mode: str = "classic", speed: str = "normal",
                 walls: bool = True):
        self._mode  = mode
        self._speed = speed
        self._walls = walls
        self._next_id = 0

        self.clients: Dict[str, Client] = {}
        self.sessions: dict = {}
        self.game:  Optional[GameData]      = None
        self.logic: Optional[SnakeGameLogic] = None
        self._reset_game()

    # ------------------------------------------------------------------ init

    def _reset_game(self, old: Optional[GameData] = None):
        mode            = old.mode            if old else self._mode
        speed           = old.speed           if old else self._speed
        walls           = old.walls_enabled   if old else self._walls
        shrink          = getattr(old, "shrinking_walls_enabled", True) if old else True
        enabled_weapons = set(getattr(old, "enabled_weapons", _ALL_WEAPONS)) if old else set(_ALL_WEAPONS)

        self.logic = SnakeGameLogic(
            player_id="server", player_name="Server", is_host=True,
            mode=mode, speed=speed, walls_enabled=walls,
            width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
        )
        self.game = GameData(
            state=GameState.WAITING.value,
            mode=mode, speed=speed, walls_enabled=walls,
            width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
            host_id="server",
            next_weapon_spawn=time.time() + random.uniform(
                WEAPON_SPAWN_MIN, WEAPON_SPAWN_MAX),
        )
        # Extra web-only settings (not in GameData dataclass)
        self.game.shrinking_walls_enabled = shrink
        self.game.enabled_weapons         = enabled_weapons
        # Clear sessions on game reset
        if hasattr(self, 'sessions'):
            self.sessions.clear()

    def _new_player_id(self) -> str:
        self._next_id += 1
        return f"wp_{self._next_id}"

    def _active_players(self) -> int:
        return sum(1 for c in self.clients.values()
                   if c.joined and not c.spectator)

    # ------------------------------------------------------------------ state

    def _filter_state(self, viewer_id: str) -> dict:
        """Return game state with invisible snakes hidden from non-owners."""
        d = asdict(self.game)
        for pid, snake in d.get("snakes", {}).items():
            if snake.get("is_invisible", False) and pid != viewer_id:
                snake["body"] = []
        # Inject extra web-only fields not present in GameData dataclass
        d["shrinking_walls_enabled"] = getattr(self.game, "shrinking_walls_enabled", True)
        d["enabled_weapons"]         = sorted(getattr(self.game, "enabled_weapons", _ALL_WEAPONS))
        d["auto_restart_in"]         = getattr(self.game, "auto_restart_in", None)
        # Time remaining in the current game (seconds), None when not running
        started_at = getattr(self.game, "_game_started_at", None)
        if started_at and self.game.state == GameState.RUNNING.value:
            d["time_remaining"] = max(0, 600 - int(time.time() - started_at))
        else:
            d["time_remaining"] = None
        return d

    async def broadcast_state(self):
        dead = []
        for pid, client in list(self.clients.items()):
            msg = {"type": "state", "game": self._filter_state(pid)}
            try:
                await client.ws.send_str(json.dumps(msg))
            except Exception:
                dead.append(pid)
        for pid in dead:
            await self._remove_client(pid)

    async def _send(self, player_id: str, msg: dict):
        c = self.clients.get(player_id)
        if c:
            await c.send(msg)

    # ------------------------------------------------------------------ clients

    async def _remove_client(self, player_id: str):
        if player_id in self.clients:
            name = self.clients[player_id].name or player_id
            del self.clients[player_id]
            log.info(f"Client left: {name} ({player_id})")
        if self.game and player_id in self.game.snakes:
            if self.game.state == GameState.WAITING.value:
                del self.game.snakes[player_id]
            else:
                self.game.snakes[player_id]["alive"] = False
        # Record disconnected_at in session instead of deleting
        for token, session in self.sessions.items():
            if session.get("player_id") == player_id:
                session["disconnected_at"] = time.time()
                break

    # ------------------------------------------------------------------ message handlers

    async def handle_join(self, player_id: str, msg: dict):
        client = self.clients.get(player_id)
        if not client:
            return

        # Anti-cheat: ignore if already joined
        if client.joined:
            return

        raw_name = str(msg.get("name", "Player"))
        name = "".join(c for c in raw_name if c.isprintable())[:16].strip() or "Player"

        # Anti-cheat: reject duplicate session token
        incoming_token = msg.get("token")
        if incoming_token and incoming_token in self.sessions:
            existing_pid = self.sessions[incoming_token].get("player_id")
            if existing_pid and existing_pid != player_id and existing_pid in self.clients:
                log.warning(f"Duplicate join attempt with token from {player_id}")
                return

        can_play = (
            self.game.state == GameState.WAITING.value
            and self._active_players() < MAX_PLAYERS
        )

        if can_play:
            token = secrets.token_hex(16)
            client.name     = name
            client.joined   = True
            client.spectator = False
            self.sessions[token] = {"name": name, "player_id": player_id, "disconnected_at": None}
            self.logic._add_player_to_game(self.game, player_id, name)
            await self._send(player_id, {
                "type": "welcome",
                "player_id": player_id,
                "spectator": False,
                "player_name": name,
                "token": token,
                "message": f"Welcome {name}!",
            })
            log.info(f"Player '{name}' joined ({player_id}), "
                     f"{self._active_players()}/{MAX_PLAYERS} players")
        else:
            client.spectator = True
            await self._send(player_id, {
                "type": "welcome",
                "player_id": player_id,
                "spectator": True,
                "message": "Game in progress – spectating",
            })
            log.info(f"Spectator joined ({player_id})")

        await self.broadcast_state()

    async def handle_rejoin(self, player_id: str, msg: dict):
        token = msg.get("token")
        if not token or token not in self.sessions:
            await self._send(player_id, {"type": "rejoin_failed"})
            return

        session = self.sessions[token]
        if self.game.state != GameState.WAITING.value:
            await self._send(player_id, {"type": "rejoin_failed"})
            return

        client = self.clients.get(player_id)
        if not client or client.joined:
            return

        name = session["name"]
        client.name      = name
        client.joined    = True
        client.spectator = False

        # Update session with new player_id
        session["player_id"]       = player_id
        session["disconnected_at"] = None

        self.logic._add_player_to_game(self.game, player_id, name)
        await self._send(player_id, {
            "type": "welcome",
            "player_id": player_id,
            "spectator": False,
            "player_name": name,
            "token": token,
            "message": f"Welcome back {name}!",
        })
        log.info(f"Player '{name}' rejoined ({player_id})")
        await self.broadcast_state()

    async def handle_start(self, player_id: str):
        if (self.game.state == GameState.WAITING.value
                and len(self.game.snakes) > 0):
            self.game.state          = GameState.COUNTDOWN.value
            self.game.countdown      = 5
            self.game.countdown_start = time.time()
            log.info("Countdown started")

    async def handle_input(self, player_id: str, msg: dict):
        client = self.clients.get(player_id)
        if not client or client.spectator:
            return

        # Anti-cheat: ignore inputs when game is not running
        if self.game.state != GameState.RUNNING.value:
            return

        action = msg.get("action", "")
        if action not in {"UP", "DOWN", "LEFT", "RIGHT", "FIRE"}:
            return

        if player_id not in self.game.snakes:
            return

        snake = self.game.snakes[player_id]
        if not snake.get("alive", False):
            return

        d = Direction(snake["direction"])
        if   action == "UP"    and d != Direction.DOWN:  snake["direction"] = Direction.UP.value
        elif action == "DOWN"  and d != Direction.UP:    snake["direction"] = Direction.DOWN.value
        elif action == "LEFT"  and d != Direction.RIGHT: snake["direction"] = Direction.LEFT.value
        elif action == "RIGHT" and d != Direction.LEFT:  snake["direction"] = Direction.RIGHT.value
        elif action == "FIRE":
            self.logic._fire_weapon(self.game, snake, time.time())

    async def handle_restart(self, player_id: str):
        if self.game.state != GameState.FINISHED.value:
            return

        # Anti-spam: only allow once per 2 seconds
        now = time.time()
        if now - getattr(self, '_last_restart_attempt', 0) < 2.0:
            return
        self._last_restart_attempt = now

        old = self.game
        self._reset_game(old)

        for pid, client in list(self.clients.items()):
            if client.joined and not client.spectator:
                self.logic._add_player_to_game(self.game, pid, client.name)
                log.info(f"Re-added '{client.name}' after restart")

        await self.broadcast_state()
        log.info("Game restarted – back to WAITING")

    async def handle_settings(self, player_id: str, msg: dict):
        """Allow any lobby player to change game settings before start.

        All setting values MUST arrive as integers (see _MODE_MAP / _SPEED_MAP).
        The server maps integers to canonical strings internally so no
        user-supplied string ever enters the game engine.
        """
        client = self.clients.get(player_id)
        if not client or client.spectator:
            return
        if self.game.state != GameState.WAITING.value:
            return   # settings locked once game starts

        # Per-client rate-limit: at most one settings change per 0.5 s
        now = time.time()
        if now - getattr(client, "_last_settings", 0) < 0.5:
            return
        client._last_settings = now

        changed = False

        # --- mode (integer index → canonical string) ----------------------
        raw_mode = msg.get("mode")
        if raw_mode is not None:
            try:
                mode = _MODE_MAP[int(raw_mode)]
            except (KeyError, TypeError, ValueError):
                mode = None
            if mode and mode != self.game.mode:
                self.game.mode = mode
                self.logic.mode = mode
                changed = True
                log.info(f"Mode → {mode} (by {client.name})")

        # --- speed (integer index → canonical string) ---------------------
        raw_speed = msg.get("speed")
        if raw_speed is not None:
            try:
                speed = _SPEED_MAP[int(raw_speed)]
            except (KeyError, TypeError, ValueError):
                speed = None
            if speed and speed != self.game.speed:
                self.game.speed = speed
                self.logic.speed = speed
                changed = True
                log.info(f"Speed → {speed} (by {client.name})")

        # --- walls (integer 0/1 only) -------------------------------------
        raw_walls = msg.get("walls")
        if raw_walls is not None:
            try:
                walls_int = int(raw_walls)
                if walls_int not in (0, 1):
                    raise ValueError
                walls_bool = bool(walls_int)
            except (TypeError, ValueError):
                walls_bool = None
            if walls_bool is not None and walls_bool != self.game.walls_enabled:
                self.game.walls_enabled = walls_bool
                self.logic.walls_enabled = walls_bool
                changed = True
                log.info(f"Walls → {walls_bool} (by {client.name})")

        # --- shrinking walls (integer 0/1 only) ---------------------------
        raw_shrink = msg.get("shrinking_walls")
        if raw_shrink is not None:
            try:
                shrink_int = int(raw_shrink)
                if shrink_int not in (0, 1):
                    raise ValueError
                shrink_bool = bool(shrink_int)
            except (TypeError, ValueError):
                shrink_bool = None
            if shrink_bool is not None and shrink_bool != getattr(self.game, "shrinking_walls_enabled", True):
                self.game.shrinking_walls_enabled = shrink_bool
                changed = True
                log.info(f"Shrinking walls → {shrink_bool} (by {client.name})")

        # --- weapon toggle (integer 0-3 + enabled 0/1) -------------------
        raw_weapon  = msg.get("weapon")
        raw_wstate  = msg.get("weapon_enabled")
        if raw_weapon is not None and raw_wstate is not None:
            try:
                weapon_name = _WEAPON_MAP[int(raw_weapon)]
                w_on_int = int(raw_wstate)
                if w_on_int not in (0, 1):
                    raise ValueError
                w_on = bool(w_on_int)
            except (KeyError, TypeError, ValueError):
                weapon_name = None
                w_on = None
            if weapon_name is not None:
                ew = getattr(self.game, "enabled_weapons", set(_ALL_WEAPONS))
                was_on = weapon_name in ew
                if w_on and not was_on:
                    ew.add(weapon_name)
                    self.game.enabled_weapons = ew
                    changed = True
                    log.info(f"Weapon {weapon_name} → enabled (by {client.name})")
                elif not w_on and was_on:
                    ew.discard(weapon_name)
                    self.game.enabled_weapons = ew
                    changed = True
                    log.info(f"Weapon {weapon_name} → disabled (by {client.name})")

        if changed:
            await self.broadcast_state()

    async def handle_message(self, player_id: str, data: str):
        try:
            msg = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return

        t = msg.get("type", "")
        if   t == "join":     await self.handle_join(player_id, msg)
        elif t == "rejoin":   await self.handle_rejoin(player_id, msg)
        elif t == "start":    await self.handle_start(player_id)
        elif t == "input":    await self.handle_input(player_id, msg)
        elif t == "restart":  await self.handle_restart(player_id)
        elif t == "settings": await self.handle_settings(player_id, msg)

    # ------------------------------------------------------------------ game loop

    async def game_loop(self):
        last_tick          = time.time()
        last_waiting_bcast = time.time()
        last_session_cleanup = time.time()

        while True:
            now = time.time()
            tick_rate = SPEED_SETTINGS.get(self.game.speed, 0.15)

            # Clean up stale disconnected sessions (older than 120 s)
            if now - last_session_cleanup >= 1.0:
                stale = [t for t, s in list(self.sessions.items())
                         if s.get("disconnected_at") and now - s["disconnected_at"] > 120]
                for t in stale:
                    del self.sessions[t]
                last_session_cleanup = now

            # Countdown
            if self.game.state == GameState.COUNTDOWN.value:
                elapsed = now - self.game.countdown_start
                new_cd  = 5 - int(elapsed)
                if new_cd != self.game.countdown:
                    self.game.countdown = max(0, new_cd)
                if elapsed >= 5.0:
                    self.game.state    = GameState.RUNNING.value
                    self.game.countdown = 0
                    log.info("Game running!")

            # Track game start time
            if self.game.state == GameState.RUNNING.value:
                if getattr(self.game, "_game_started_at", None) is None:
                    self.game._game_started_at = now

            # 10-minute game time limit — force game over
            if self.game.state == GameState.RUNNING.value:
                started_at = getattr(self.game, "_game_started_at", None)
                if started_at and (now - started_at) >= 600.0:
                    log.info("10-minute time limit reached — ending game")
                    # Kill all remaining snakes to trigger normal game-over flow
                    for pid, snake in list(self.game.snakes.items()):
                        if snake.get("alive"):
                            snake["alive"] = False
                    await self._tick(now)   # process deaths / winner calc

            # Auto-restart after FINISHED state lingers 10 s
            if self.game.state == GameState.FINISHED.value:
                finished_at = getattr(self.game, "_finished_at", None)
                if finished_at is None:
                    self.game._finished_at = now
                    finished_at = now
                remaining = max(0, 10 - int(now - finished_at))
                if remaining != getattr(self.game, "auto_restart_in", None):
                    self.game.auto_restart_in = remaining
                    await self.broadcast_state()
                if now - finished_at >= 10.0:
                    log.info("Auto-restarting after 10 s timeout")
                    await self.handle_restart(None)   # None = server-triggered

            # Tick
            if now - last_tick >= tick_rate:
                if self.game.state == GameState.RUNNING.value:
                    await self._tick(now)
                await self.broadcast_state()
                last_tick = now
            elif self.game.state == GameState.WAITING.value:
                if now - last_waiting_bcast >= 0.5:
                    await self.broadcast_state()
                    last_waiting_bcast = now

            await asyncio.sleep(0.005)

    async def _tick(self, now: float):
        g = self.game

        # Expire ghost invisibility
        for snake in g.snakes.values():
            if snake.get("is_invisible") and now >= snake.get("invisible_until", 0):
                snake["is_invisible"] = False

        # Move
        for snake in g.snakes.values():
            if snake.get("alive"):
                self.logic._move_snake(g, snake)

        # Weapons / collisions
        self.logic._update_shotgun_bursts(g, now)
        self.logic._update_bombs(g)
        self.logic._update_explosions(g)
        self.logic._check_collisions(g)
        self.logic._update_rankings(g)

        # Spawn weapons — only those enabled in lobby settings
        if now >= g.next_weapon_spawn:
            alive = max(1, sum(1 for s in g.snakes.values() if s["alive"]))
            enabled = getattr(g, "enabled_weapons", _ALL_WEAPONS)
            _w_weights = {"bomb": 1.0, "ghost": 1.0, "shotgun": 1.0, "nuclear": 0.3}
            weapon_pool = [w for w in ("bomb", "ghost", "shotgun", "nuclear") if w in enabled]
            if weapon_pool:
                weights = [_w_weights[w] for w in weapon_pool]
                choice = random.choices(weapon_pool, weights=weights)[0]
                {
                    "bomb":    self.logic._spawn_weapon,
                    "ghost":   self.logic._spawn_ghost,
                    "shotgun": self.logic._spawn_shotgun,
                    "nuclear": self.logic._spawn_nuclear,
                }[choice](g)
                interval = random.uniform(WEAPON_SPAWN_MIN, WEAPON_SPAWN_MAX) / alive
                g.next_weapon_spawn = now + max(1.0, interval)
            else:
                # No weapons enabled — recheck after 5 s
                g.next_weapon_spawn = now + 5.0

        # Shrinking walls — only if walls are enabled AND shrinking is enabled
        alive = sum(1 for s in g.snakes.values() if s["alive"])

        if getattr(g, "shrinking_walls_enabled", True) and g.walls_enabled:
            if alive <= 3 and alive > 0 and not g.shrinking_walls_active:
                g.shrinking_walls_active = True
                g.shrinking_wall_bounds = {
                    "top": 0, "bottom": g.height - 1,
                    "left": 0, "right": g.width - 1,
                }
                g.last_wall_shrink = now
                log.info(f"Shrinking walls activated ({alive} players left)")

            if g.shrinking_walls_active and alive > 0:
                shrink_interval = 15.0 if alive == 3 else 10.0
                if now - g.last_wall_shrink >= shrink_interval:
                    self.logic._shrink_walls(g)
                    g.last_wall_shrink = now

        # Game over: last one standing wins (also ends solo games when player dies)
        if alive == 0 or (alive <= 1 and len(g.snakes) > 1):
            g.state = GameState.FINISHED.value
            for pid, s in g.snakes.items():
                if s["alive"]:
                    g.winner = s["player_name"]
                    if not any(r["player_id"] == pid for r in g.player_rankings):
                        g.player_rankings.insert(0, {
                            "player_id":   pid,
                            "player_name": s["player_name"],
                            "rank": 1,
                            "score": s["score"],
                        })
            for i, r in enumerate(g.player_rankings):
                r["rank"] = i + 1
            log.info(f"Game over! Winner: {g.winner}")

        g.tick += 1

    # ------------------------------------------------------------------ WebSocket handler

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        # Max connections per IP
        ip = request.remote
        ip_count = sum(1 for c in self.clients.values() if getattr(c, 'ip', None) == ip)
        if ip_count >= 3:
            await ws.close(code=4029, message=b"Too many connections from this IP")
            return ws

        player_id = self._new_player_id()
        client = Client(ws, player_id)
        client.ip = ip
        self.clients[player_id] = client
        log.info(f"WS connected: {player_id} from {request.remote}")

        # Send current state immediately so client can show lobby / spectate
        await self.clients[player_id].send({
            "type": "state",
            "game": self._filter_state(player_id),
        })

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    # Message size limit
                    if len(msg.data) > 1024:
                        log.warning(f"Oversized message from {player_id} ({len(msg.data)} bytes) — dropping")
                        continue
                    # Rate limiting
                    now = time.time()
                    client.msg_timestamps = [t for t in client.msg_timestamps if now - t < 1.0]
                    if len(client.msg_timestamps) > 60:
                        continue  # drop — too fast
                    client.msg_timestamps.append(now)
                    await self.handle_message(player_id, msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR,
                                  aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            await self._remove_client(player_id)

        return ws


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def make_app(server: "WebSnakeServer") -> web.Application:
    app = web.Application()

    # WebSocket endpoint (must be registered before the catch-all)
    app.router.add_get("/ws", server.ws_handler)

    # Static file handler — serves everything else from the web/ directory
    web_dir = os.path.dirname(os.path.abspath(__file__))

    async def serve_static(request: web.Request) -> web.Response:
        # Strip leading slash; empty path → index.html
        rel = request.path.lstrip("/") or "index.html"
        # Safety: prevent directory traversal
        filepath = os.path.normpath(os.path.join(web_dir, rel))
        try:
            common = os.path.commonpath([filepath, web_dir])
        except ValueError:
            raise web.HTTPForbidden()
        if common != web_dir:
            raise web.HTTPForbidden()
        # Only serve known file extensions
        allowed_exts = {'.html', '.js', '.css', '.ico', '.png', '.woff2', ''}
        _, ext = os.path.splitext(filepath)
        if ext.lower() not in allowed_exts:
            raise web.HTTPForbidden()
        if os.path.isfile(filepath):
            return web.FileResponse(filepath)
        # Fallback: always serve index.html (single-page app)
        return web.FileResponse(os.path.join(web_dir, "index.html"))

    # Explicit "/" route first (aiohttp may not match /{path:.*} on bare /)
    app.router.add_get("/",         serve_static)
    app.router.add_get("/{path:.*}", serve_static)

    async def _start_loop(app):
        asyncio.create_task(server.game_loop())

    app.on_startup.append(_start_loop)
    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Snake Web Server")
    parser.add_argument("--host",     default="0.0.0.0")
    parser.add_argument("--port",     type=int, default=8765)
    parser.add_argument("--mode",     choices=["classic", "kurve"], default="classic")
    parser.add_argument("--speed",    choices=["normal", "fast", "ultra"], default="normal")
    parser.add_argument("--no-walls", action="store_true")
    args = parser.parse_args()

    srv = WebSnakeServer(
        mode=args.mode,
        speed=args.speed,
        walls=not args.no_walls,
    )

    async def main():
        app     = make_app(srv)
        runner  = web.AppRunner(app)
        await runner.setup()
        site    = web.TCPSite(runner, args.host, args.port)
        await site.start()
        log.info(f"Server:  http://{args.host}:{args.port}")
        log.info(f"Browser: http://localhost:{args.port}")
        await asyncio.Future()  # run forever

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server stopped.")
