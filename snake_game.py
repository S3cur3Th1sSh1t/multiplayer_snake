#!/usr/bin/env python3
"""
Multi-User Snake Game with TCP/UDP Networking
==============================================
A multiplayer snake game supporting up to 10 players via network.
Players can connect from different machines over the network.

Features:
- Hybrid TCP/UDP architecture for optimal performance:
  - TCP: Reliable messages (join, inputs, start, restart)
  - UDP: Fast game state broadcasts (low latency for smooth animation)
- Multi-user support (up to 10 players)
- Two game modes: Classic Snake and "Achtung die Kurve"
- Weapon system with bombs that destroy snake segments
- Ghost mode for temporary invisibility
- Collision detection between snakes
- Speed modes: normal, fast, ultra
- Optional wall wrap-around
- GUI mode with pygame

Usage - Network Mode (RECOMMENDED):
    Server:  python3 snake_game.py --server [--ip 0.0.0.0] [--port 5555] [--mode classic|kurve] [--speed normal|fast|ultra]
    Client:  python3 snake_game.py --connect HOST:PORT --gui [--name PlayerName]
    Client:  python3 snake_game.py --connect HOST:PORT [--name PlayerName]  (terminal mode)

Usage - Legacy File-Based Mode (same machine only):
    Host:   python3 snake_game.py --host [--mode classic|kurve] [--gui]
    Player: python3 snake_game.py --join [--gui]

Controls:
    Arrow Keys: Move snake
    Space: Fire bomb (if available)
    G: Activate ghost mode (if available)
    S: Start game (any player can start)
    R: Restart game (after game over)
    Q/ESC: Quit
"""

import time
import random
import os
import sys
import json
import argparse
import signal
import hashlib
import socket
import select
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Tuple, Optional, Dict
from enum import Enum
import threading
import math

# Platform-specific imports
IS_WINDOWS = sys.platform == 'win32'
if not IS_WINDOWS:
    import curses
    import fcntl
else:
    curses = None
    fcntl = None

# Check for pygame availability
PYGAME_AVAILABLE = False
try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    pass

# Game configuration
if IS_WINDOWS:
    GAME_DIR = os.path.join(os.environ.get('TEMP', 'C:\\temp'), 'snake_game')
else:
    GAME_DIR = "/tmp/snake_game"
GAME_STATE_FILE = os.path.join(GAME_DIR, "game_state.json")
PLAYER_INPUT_DIR = os.path.join(GAME_DIR, "inputs")
LOCK_FILE = os.path.join(GAME_DIR, "game.lock")

MAX_PLAYERS = 10
WEAPON_SPAWN_MIN = 5  # Minimum seconds between weapon spawns
WEAPON_SPAWN_MAX = 15  # Maximum seconds between weapon spawns
BOMB_RANGE = 25  # How far a bomb travels
BOMB_DAMAGE = 4  # How many blocks a bomb destroys
BOMB_SPEED = 3  # Bomb moves this many cells per tick
GHOST_DURATION = 5.0  # How long invisibility lasts in seconds

# Weapon types
WEAPON_BOMB = "bomb"
WEAPON_GHOST = "ghost"

# Speed settings (tick rate in seconds)
SPEED_SETTINGS = {
    'normal': 0.15,
    'fast': 0.05,
    'ultra': 0.02
}

# Network settings
DEFAULT_PORT = 5555
DEFAULT_UDP_PORT = 5556  # UDP port for fast game state broadcasts
DEFAULT_HOST = '0.0.0.0'  # Listen on all interfaces
BUFFER_SIZE = 65536
NETWORK_TIMEOUT = 0.01  # Non-blocking timeout
UDP_BUFFER_SIZE = 65507  # Max UDP packet size

# Security settings
MAX_MESSAGE_SIZE = 65536  # Max 64KB per message
MAX_CONNECTIONS = 50  # Max simultaneous TCP connections
MAX_MESSAGES_PER_SECOND = 30  # Rate limiting per client
PASSWORD_LENGTH = 15  # Random password length
VALID_ACTIONS = {'UP', 'DOWN', 'LEFT', 'RIGHT', 'FIRE', 'GHOST'}  # Valid player actions

# Private IP ranges (RFC 1918 + localhost)
PRIVATE_IP_PREFIXES = ('10.', '172.16.', '172.17.', '172.18.', '172.19.',
                       '172.20.', '172.21.', '172.22.', '172.23.', '172.24.',
                       '172.25.', '172.26.', '172.27.', '172.28.', '172.29.',
                       '172.30.', '172.31.', '192.168.', '127.', 'localhost')

# Global debug flag (as list to allow modification from functions)
_DEBUG = [False]

def debug_print(*args, **kwargs):
    """Print debug messages only if debug mode is enabled"""
    if _DEBUG[0]:
        print("DEBUG:", *args, **kwargs)

def set_debug_mode(enabled: bool):
    """Enable or disable debug mode"""
    _DEBUG[0] = enabled


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """Generate a random alphanumeric password"""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def is_private_ip(ip: str) -> bool:
    """Check if an IP address is private/local"""
    if ip in ('0.0.0.0', '::'):
        return False  # Binding to all interfaces = potentially public
    return ip.startswith(PRIVATE_IP_PREFIXES)


# Colors for players (curses color pairs) - only used on non-Windows
PLAYER_COLORS = []
if not IS_WINDOWS and curses:
    PLAYER_COLORS = [
        curses.COLOR_GREEN,
        curses.COLOR_RED,
        curses.COLOR_BLUE,
        curses.COLOR_YELLOW,
        curses.COLOR_MAGENTA,
        curses.COLOR_CYAN,
        curses.COLOR_WHITE,
        curses.COLOR_GREEN,
        curses.COLOR_RED,
        curses.COLOR_BLUE,
    ]

# GUI Colors (RGB)
GUI_COLORS = [
    (0, 255, 0),      # Green
    (255, 0, 0),      # Red
    (0, 100, 255),    # Blue
    (255, 255, 0),    # Yellow
    (255, 0, 255),    # Magenta
    (0, 255, 255),    # Cyan
    (255, 255, 255),  # White
    (100, 255, 100),  # Light Green
    (255, 100, 100),  # Light Red
    (100, 100, 255),  # Light Blue
]

# Symbols for terminal mode
SNAKE_HEAD = "@"
SNAKE_BODY = "o"
DEAD_SNAKE = "x"
FOOD_SYMBOL = "*"
WEAPON_SYMBOL = "W"
GHOST_SYMBOL = "G"
BOMB_SYMBOL = "!"
WALL_SYMBOL = "#"
EXPLOSION_SYMBOL = "X"


class Direction(Enum):
    UP = 0
    DOWN = 1
    LEFT = 2
    RIGHT = 3


class GameMode(Enum):
    CLASSIC = "classic"
    KURVE = "kurve"


class GameState(Enum):
    WAITING = "waiting"
    COUNTDOWN = "countdown"
    RUNNING = "running"
    FINISHED = "finished"


@dataclass
class GameData:
    state: str = "waiting"
    mode: str = "classic"
    speed: str = "normal"
    walls_enabled: bool = True
    width: int = 80
    height: int = 24
    snakes: Dict = field(default_factory=dict)
    foods: List[List[int]] = field(default_factory=list)
    weapons: List[List[int]] = field(default_factory=list)  # Bomb pickups: [x, y]
    ghost_pickups: List[List[int]] = field(default_factory=list)  # Ghost pickups: [x, y]
    bombs: List[Dict] = field(default_factory=list)
    explosions: List[Dict] = field(default_factory=list)  # For visual effects
    host_id: str = ""
    next_player_id: int = 0
    tick: int = 0
    last_weapon_spawn: float = 0
    next_weapon_spawn: float = 0
    winner: str = ""
    countdown: int = 0  # Countdown value (5, 4, 3, 2, 1, 0)
    countdown_start: float = 0  # Timestamp when countdown started


def ensure_game_dir():
    """Create game directory structure with proper permissions for multi-user access"""
    # Create directories with world-writable permissions
    old_umask = os.umask(0)  # Temporarily set umask to 0 for full permissions
    try:
        os.makedirs(GAME_DIR, mode=0o777, exist_ok=True)
        os.makedirs(PLAYER_INPUT_DIR, mode=0o777, exist_ok=True)
    finally:
        os.umask(old_umask)  # Restore original umask
    
    # Ensure permissions are correct (in case dirs already existed)
    try:
        os.chmod(GAME_DIR, 0o777)
        os.chmod(PLAYER_INPUT_DIR, 0o777)
    except:
        pass
    
    # Create lock file with proper permissions if it doesn't exist
    try:
        if not os.path.exists(LOCK_FILE):
            old_umask = os.umask(0)
            try:
                with open(LOCK_FILE, 'w') as f:
                    pass
            finally:
                os.umask(old_umask)
        os.chmod(LOCK_FILE, 0o666)
    except:
        pass


def get_user_id() -> str:
    """Get unique identifier for current user/session"""
    user = os.environ.get('USER', os.environ.get('USERNAME', 'unknown'))
    if IS_WINDOWS:
        tty = str(os.getpid())
    else:
        tty = os.ttyname(sys.stdin.fileno()) if sys.stdin.isatty() else str(os.getpid())
    return hashlib.md5(f"{user}:{tty}:{os.getpid()}".encode()).hexdigest()[:8]


def get_username() -> str:
    """Get current username"""
    return os.environ.get('USER', os.environ.get('USERNAME', 'Player'))


class FileLock:
    """Simple file-based locking - cross-platform with multi-user support"""
    def __init__(self, filepath):
        self.filepath = filepath
        self.fd = None
    
    def acquire(self):
        if IS_WINDOWS:
            # On Windows, use a simple file existence check with retries
            import time as t
            lock_file = self.filepath + ".lock"
            max_attempts = 50
            for _ in range(max_attempts):
                try:
                    # Try to create lock file exclusively
                    self.fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    return
                except FileExistsError:
                    t.sleep(0.01)
                except OSError:
                    t.sleep(0.01)
            # If we couldn't get lock, proceed anyway (best effort)
            self.fd = None
        else:
            # Create lock file with world-writable permissions if it doesn't exist
            if not os.path.exists(self.filepath):
                old_umask = os.umask(0)
                try:
                    with open(self.filepath, 'w') as f:
                        pass
                    os.chmod(self.filepath, 0o666)
                except:
                    pass
                finally:
                    os.umask(old_umask)
            
            self.fd = open(self.filepath, 'r+' if os.path.exists(self.filepath) else 'w')
            try:
                os.chmod(self.filepath, 0o666)
            except:
                pass
            fcntl.flock(self.fd, fcntl.LOCK_EX)
    
    def release(self):
        if IS_WINDOWS:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                    os.remove(self.filepath + ".lock")
                except:
                    pass
                self.fd = None
        else:
            if self.fd:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                self.fd.close()
                self.fd = None
    
    def __enter__(self):
        self.acquire()
        return self
    
    def __exit__(self, *args):
        self.release()


def load_game_state() -> Optional[GameData]:
    """Load game state from file"""
    try:
        if os.path.exists(GAME_STATE_FILE):
            with open(GAME_STATE_FILE, 'r') as f:
                data = json.load(f)
                return GameData(**data)
    except (json.JSONDecodeError, FileNotFoundError, TypeError):
        pass
    return None


def save_game_state(game: GameData):
    """Save game state to file"""
    old_umask = os.umask(0)
    try:
        with open(GAME_STATE_FILE, 'w') as f:
            json.dump(asdict(game), f)
    finally:
        os.umask(old_umask)
    try:
        os.chmod(GAME_STATE_FILE, 0o666)
    except:
        pass


def send_input(player_id: str, action: str):
    """Send player input via file"""
    input_file = os.path.join(PLAYER_INPUT_DIR, f"{player_id}.input")
    # Use umask to ensure file is created with proper permissions
    old_umask = os.umask(0)
    try:
        with open(input_file, 'w') as f:
            f.write(action)
    finally:
        os.umask(old_umask)
    try:
        os.chmod(input_file, 0o666)
    except:
        pass


def read_inputs() -> Dict[str, str]:
    """Read all player inputs"""
    inputs = {}
    try:
        for filename in os.listdir(PLAYER_INPUT_DIR):
            if filename.endswith('.input'):
                player_id = filename[:-6]
                filepath = os.path.join(PLAYER_INPUT_DIR, filename)
                try:
                    with open(filepath, 'r') as f:
                        action = f.read().strip()
                        if action:
                            inputs[player_id] = action
                    with open(filepath, 'w') as f:
                        f.write("")
                except:
                    pass
    except FileNotFoundError:
        pass
    return inputs


def clear_inputs():
    """Clear all input files"""
    try:
        for filename in os.listdir(PLAYER_INPUT_DIR):
            if filename.endswith('.input'):
                filepath = os.path.join(PLAYER_INPUT_DIR, filename)
                try:
                    os.remove(filepath)
                except:
                    pass
    except FileNotFoundError:
        pass


class SnakeGameLogic:
    """Shared game logic for both terminal and GUI modes"""
    
    def __init__(self, player_id: str, player_name: str, is_host: bool, 
                 mode: str, speed: str, walls_enabled: bool, width: int, height: int):
        self.player_id = player_id
        self.player_name = player_name
        self.is_host = is_host
        self.mode = mode
        self.speed = speed
        self.walls_enabled = walls_enabled
        self.width = width
        self.height = height
        self.tick_rate = SPEED_SETTINGS.get(speed, 0.15)
    
    def init_host_game(self) -> GameData:
        """Initialize a new game as host"""
        ensure_game_dir()
        clear_inputs()
        
        # Remove old game state file to ensure fresh start
        try:
            if os.path.exists(GAME_STATE_FILE):
                os.remove(GAME_STATE_FILE)
        except:
            pass
        
        game = GameData(
            state=GameState.WAITING.value,
            mode=self.mode,
            speed=self.speed,
            walls_enabled=self.walls_enabled,
            width=self.width,
            height=self.height,
            host_id=self.player_id,
            next_weapon_spawn=time.time() + random.uniform(WEAPON_SPAWN_MIN, WEAPON_SPAWN_MAX)
        )
        
        self._add_player_to_game(game, self.player_id, self.player_name)
        
        with FileLock(LOCK_FILE):
            save_game_state(game)
        
        return game
    
    def join_game(self) -> Optional[GameData]:
        """Join an existing game"""
        # Ensure directories exist (in case joining before host fully initialized)
        ensure_game_dir()
        
        with FileLock(LOCK_FILE):
            game = load_game_state()
            if game is None:
                return None
            
            if game.state != GameState.WAITING.value:
                return None
            
            if len(game.snakes) >= MAX_PLAYERS:
                return None
            
            if self.player_id not in game.snakes:
                self._add_player_to_game(game, self.player_id, self.player_name)
                save_game_state(game)
            
            # Update local settings from game
            self.speed = game.speed
            self.walls_enabled = game.walls_enabled
            self.tick_rate = SPEED_SETTINGS.get(game.speed, 0.15)
            
            return game
    
    def _add_player_to_game(self, game: GameData, player_id: str, player_name: str):
        """Add a new player to the game"""
        player_num = game.next_player_id
        game.next_player_id += 1
        
        # Find a random starting position that doesn't collide with other snakes
        max_attempts = 100
        start_x, start_y = 0, 0
        direction = random.choice([Direction.UP, Direction.DOWN, Direction.LEFT, Direction.RIGHT])
        
        for _ in range(max_attempts):
            # Random position with some margin from walls
            start_x = random.randint(5, game.width - 5)
            start_y = random.randint(5, game.height - 5)
            
            # Check if this position is clear (including space for initial body)
            position_clear = True
            for check_offset in range(-3, 4):  # Check a small area around the start
                for dy in range(-1, 2):
                    check_x = start_x + check_offset
                    check_y = start_y + dy
                    if self._is_position_occupied(game, check_x, check_y):
                        position_clear = False
                        break
                if not position_clear:
                    break
            
            if position_clear:
                break
        
        # Create initial body based on random direction
        if direction == Direction.RIGHT:
            initial_body = [[start_x, start_y], [start_x - 1, start_y], [start_x - 2, start_y]]
        elif direction == Direction.LEFT:
            initial_body = [[start_x, start_y], [start_x + 1, start_y], [start_x + 2, start_y]]
        elif direction == Direction.DOWN:
            initial_body = [[start_x, start_y], [start_x, start_y - 1], [start_x, start_y - 2]]
        else:  # UP
            initial_body = [[start_x, start_y], [start_x, start_y + 1], [start_x, start_y + 2]]
        
        snake_data = {
            'player_id': player_num,
            'player_name': player_name,
            'body': initial_body,
            'direction': direction.value,
            'alive': True,
            'has_weapon': False,  # Bomb weapon
            'has_ghost': False,   # Ghost weapon
            'is_invisible': False,
            'invisible_until': 0,  # Timestamp when invisibility ends
            'score': 0,
            'color': player_num
        }
        
        game.snakes[player_id] = snake_data
        self._spawn_food(game)
    
    def _spawn_food(self, game: GameData):
        """Spawn a food item at random position"""
        attempts = 100
        while attempts > 0:
            x = random.randint(2, game.width - 2)
            y = random.randint(2, game.height - 2)
            
            if not self._is_position_occupied(game, x, y):
                game.foods.append([x, y])
                return
            attempts -= 1
    
    def _spawn_weapon(self, game: GameData):
        """Spawn a weapon pickup (bomb)"""
        attempts = 100
        while attempts > 0:
            x = random.randint(2, game.width - 2)
            y = random.randint(2, game.height - 2)
            
            if not self._is_position_occupied(game, x, y):
                game.weapons.append([x, y])
                return
            attempts -= 1
    
    def _spawn_ghost(self, game: GameData):
        """Spawn a ghost pickup"""
        attempts = 100
        while attempts > 0:
            x = random.randint(2, game.width - 2)
            y = random.randint(2, game.height - 2)
            
            if not self._is_position_occupied(game, x, y):
                game.ghost_pickups.append([x, y])
                return
            attempts -= 1
    
    def _is_position_occupied(self, game: GameData, x: int, y: int) -> bool:
        """Check if a position is occupied"""
        for snake_data in game.snakes.values():
            for segment in snake_data['body']:
                if segment[0] == x and segment[1] == y:
                    return True
        
        for food in game.foods:
            if food[0] == x and food[1] == y:
                return True
        
        for weapon in game.weapons:
            if weapon[0] == x and weapon[1] == y:
                return True
        
        for ghost in game.ghost_pickups:
            if ghost[0] == x and ghost[1] == y:
                return True
        
        return False
    
    def start_game(self, game: GameData):
        """Start the countdown before game begins"""
        game.state = GameState.COUNTDOWN.value
        game.countdown = 5
        game.countdown_start = time.time()
        save_game_state(game)
    
    def update_countdown(self, game: GameData) -> bool:
        """Update countdown, returns True when countdown is finished"""
        if game.state != GameState.COUNTDOWN.value:
            return False
        
        elapsed = time.time() - game.countdown_start
        new_countdown = 5 - int(elapsed)
        
        if new_countdown != game.countdown:
            game.countdown = max(0, new_countdown)
            save_game_state(game)
        
        if elapsed >= 5.0:
            game.state = GameState.RUNNING.value
            game.countdown = 0
            save_game_state(game)
            return True
        
        return False
    
    def update_game(self, game: GameData):
        """Update game state"""
        if game.state != GameState.RUNNING.value:
            return
        
        current_time = time.time()
        inputs = read_inputs()
        
        # Process inputs
        for player_id, action in inputs.items():
            if player_id in game.snakes:
                snake = game.snakes[player_id]
                if not snake['alive']:
                    continue
                
                current_dir = Direction(snake['direction'])
                
                if action == 'UP' and current_dir != Direction.DOWN:
                    snake['direction'] = Direction.UP.value
                elif action == 'DOWN' and current_dir != Direction.UP:
                    snake['direction'] = Direction.DOWN.value
                elif action == 'LEFT' and current_dir != Direction.RIGHT:
                    snake['direction'] = Direction.LEFT.value
                elif action == 'RIGHT' and current_dir != Direction.LEFT:
                    snake['direction'] = Direction.RIGHT.value
                elif action == 'FIRE' and snake.get('has_weapon'):
                    self._fire_weapon(game, snake)
                elif action == 'GHOST' and snake.get('has_ghost'):
                    self._activate_ghost(game, snake, current_time)
        
        # Update invisibility status
        for player_id, snake in game.snakes.items():
            if snake.get('is_invisible') and current_time >= snake.get('invisible_until', 0):
                snake['is_invisible'] = False
        
        # Move snakes
        for player_id, snake in game.snakes.items():
            if not snake['alive']:
                continue
            self._move_snake(game, snake)
        
        # Update bombs
        self._update_bombs(game)
        
        # Update explosions (visual effect decay)
        self._update_explosions(game)
        
        # Check collisions
        self._check_collisions(game)
        
        # Spawn weapons (alternating between bomb and ghost)
        if current_time >= game.next_weapon_spawn:
            if random.random() < 0.5:
                self._spawn_weapon(game)
            else:
                self._spawn_ghost(game)
            game.next_weapon_spawn = current_time + random.uniform(WEAPON_SPAWN_MIN, WEAPON_SPAWN_MAX)
        
        # Check winner
        alive_count = sum(1 for s in game.snakes.values() if s['alive'])
        if alive_count <= 1 and len(game.snakes) > 1:
            game.state = GameState.FINISHED.value
            for pid, s in game.snakes.items():
                if s['alive']:
                    game.winner = s['player_name']
        
        game.tick += 1
        save_game_state(game)
    
    def _move_snake(self, game: GameData, snake: dict):
        """Move a snake"""
        head_x, head_y = snake['body'][0][0], snake['body'][0][1]
        direction = Direction(snake['direction'])
        
        if direction == Direction.UP:
            new_head = [head_x, head_y - 1]
        elif direction == Direction.DOWN:
            new_head = [head_x, head_y + 1]
        elif direction == Direction.LEFT:
            new_head = [head_x - 1, head_y]
        else:
            new_head = [head_x + 1, head_y]
        
        # Wall wrap-around if walls disabled
        if not game.walls_enabled:
            if new_head[0] <= 0:
                new_head[0] = game.width - 2
            elif new_head[0] >= game.width - 1:
                new_head[0] = 1
            if new_head[1] <= 0:
                new_head[1] = game.height - 2
            elif new_head[1] >= game.height - 1:
                new_head[1] = 1
        
        snake['body'].insert(0, new_head)
        
        # Food collision
        ate_food = False
        food_to_remove = None
        for food in game.foods:
            if food[0] == new_head[0] and food[1] == new_head[1]:
                food_to_remove = food
                snake['score'] += 10
                ate_food = True
                break
        
        if food_to_remove:
            game.foods.remove(food_to_remove)
            self._spawn_food(game)
        
        # Weapon pickup (bomb)
        weapon_to_remove = None
        for weapon in game.weapons:
            if weapon[0] == new_head[0] and weapon[1] == new_head[1]:
                weapon_to_remove = weapon
                snake['has_weapon'] = True
                break
        
        if weapon_to_remove:
            game.weapons.remove(weapon_to_remove)
        
        # Ghost pickup
        ghost_to_remove = None
        for ghost in game.ghost_pickups:
            if ghost[0] == new_head[0] and ghost[1] == new_head[1]:
                ghost_to_remove = ghost
                snake['has_ghost'] = True
                break
        
        if ghost_to_remove:
            game.ghost_pickups.remove(ghost_to_remove)
        
        # Remove tail based on mode
        if game.mode == GameMode.KURVE.value:
            pass  # Always grow
        else:
            if not ate_food:
                snake['body'].pop()
    
    def _fire_weapon(self, game: GameData, snake: dict):
        """Fire a bomb"""
        snake['has_weapon'] = False
        head_x, head_y = snake['body'][0][0], snake['body'][0][1]
        direction = Direction(snake['direction'])
        
        if direction == Direction.UP:
            start_x, start_y = head_x, head_y - 1
        elif direction == Direction.DOWN:
            start_x, start_y = head_x, head_y + 1
        elif direction == Direction.LEFT:
            start_x, start_y = head_x - 1, head_y
        else:
            start_x, start_y = head_x + 1, head_y
        
        bomb = {
            'x': start_x,
            'y': start_y,
            'direction': snake['direction'],
            'owner_id': snake['player_id'],
            'remaining_range': BOMB_RANGE
        }
        game.bombs.append(bomb)
    
    def _activate_ghost(self, game: GameData, snake: dict, current_time: float):
        """Activate ghost mode (invisibility)"""
        snake['has_ghost'] = False
        snake['is_invisible'] = True
        snake['invisible_until'] = current_time + GHOST_DURATION
    
    def _update_bombs(self, game: GameData):
        """Update bomb positions and handle explosions"""
        bombs_to_remove = []
        
        for i, bomb in enumerate(game.bombs):
            bomb_exploded = False
            
            for _ in range(BOMB_SPEED):
                if bomb_exploded:
                    break
                
                direction = Direction(bomb['direction'])
                
                # Move bomb
                if direction == Direction.UP:
                    bomb['y'] -= 1
                elif direction == Direction.DOWN:
                    bomb['y'] += 1
                elif direction == Direction.LEFT:
                    bomb['x'] -= 1
                else:
                    bomb['x'] += 1
                
                bomb['remaining_range'] -= 1
                
                # Check wall collision (only if walls enabled)
                if game.walls_enabled:
                    if (bomb['x'] <= 0 or bomb['x'] >= game.width - 1 or
                        bomb['y'] <= 0 or bomb['y'] >= game.height - 1):
                        # Bomb hits wall - create explosion
                        self._create_explosion(game, bomb['x'], bomb['y'])
                        bomb_exploded = True
                        if i not in bombs_to_remove:
                            bombs_to_remove.append(i)
                        break
                else:
                    # Wrap around
                    if bomb['x'] <= 0:
                        bomb['x'] = game.width - 2
                    elif bomb['x'] >= game.width - 1:
                        bomb['x'] = 1
                    if bomb['y'] <= 0:
                        bomb['y'] = game.height - 2
                    elif bomb['y'] >= game.height - 1:
                        bomb['y'] = 1
                
                # Check snake collision (including own snake!)
                for player_id, snake in game.snakes.items():
                    hit_index = -1
                    for j, segment in enumerate(snake['body']):
                        if bomb['x'] == segment[0] and bomb['y'] == segment[1]:
                            hit_index = j
                            break
                    
                    if hit_index >= 0:
                        # Create explosion effect
                        self._create_explosion(game, bomb['x'], bomb['y'])
                        
                        # Remove BOMB_DAMAGE segments starting from hit point
                        segments_removed = 0
                        while segments_removed < BOMB_DAMAGE and hit_index < len(snake['body']) and len(snake['body']) > 1:
                            snake['body'].pop(hit_index)
                            segments_removed += 1
                        
                        # If snake is too short, it dies
                        if len(snake['body']) < 2:
                            snake['alive'] = False
                        
                        bomb_exploded = True
                        if i not in bombs_to_remove:
                            bombs_to_remove.append(i)
                        break
                
                if bomb_exploded:
                    break
                
                if bomb['remaining_range'] <= 0:
                    if i not in bombs_to_remove:
                        bombs_to_remove.append(i)
                    break
        
        # Remove spent bombs
        for i in sorted(bombs_to_remove, reverse=True):
            if i < len(game.bombs):
                game.bombs.pop(i)
    
    def _create_explosion(self, game: GameData, x: int, y: int):
        """Create an explosion effect"""
        explosion = {
            'x': x,
            'y': y,
            'radius': 3,
            'ttl': 5  # Time to live in ticks
        }
        game.explosions.append(explosion)
    
    def _update_explosions(self, game: GameData):
        """Update explosion effects"""
        explosions_to_remove = []
        for i, exp in enumerate(game.explosions):
            exp['ttl'] -= 1
            if exp['ttl'] <= 0:
                explosions_to_remove.append(i)
        
        for i in sorted(explosions_to_remove, reverse=True):
            if i < len(game.explosions):
                game.explosions.pop(i)
    
    def _check_collisions(self, game: GameData):
        """Check for snake collisions"""
        for player_id, snake in game.snakes.items():
            if not snake['alive']:
                continue
            
            head_x, head_y = snake['body'][0][0], snake['body'][0][1]
            
            # Wall collision (only if walls enabled)
            if game.walls_enabled:
                if (head_x <= 0 or head_x >= game.width - 1 or
                    head_y <= 0 or head_y >= game.height - 1):
                    snake['alive'] = False
                    continue
            
            # Self collision
            for segment in snake['body'][1:]:
                if head_x == segment[0] and head_y == segment[1]:
                    snake['alive'] = False
                    break
            
            if not snake['alive']:
                continue
            
            # Other snake collision (skip dead snakes and invisible snakes)
            for other_id, other_snake in game.snakes.items():
                if other_id == player_id:
                    continue
                
                # Don't collide with dead snakes
                if not other_snake['alive']:
                    continue
                
                # Don't collide with invisible snakes (ghost mode)
                if other_snake.get('is_invisible', False):
                    continue
                
                for segment in other_snake['body']:
                    if head_x == segment[0] and head_y == segment[1]:
                        snake['alive'] = False
                        break
                
                if not snake['alive']:
                    break


# =============================================================================
# TCP NETWORK CLASSES
# =============================================================================

class NetworkProtocol:
    """Simple length-prefixed JSON protocol for TCP communication"""
    
    @staticmethod
    def encode(message: dict) -> bytes:
        """Encode a message with length prefix for TCP"""
        data = json.dumps(message).encode('utf-8')
        length = len(data)
        return length.to_bytes(4, 'big') + data
    
    @staticmethod
    def decode_from_buffer(buffer: bytes) -> Tuple[Optional[dict], bytes]:
        """Decode a message from buffer, return (message, remaining_buffer)
        
        Returns (None, buffer) if incomplete message
        Returns (None, remaining) if message too large (skipped)
        Returns (message, remaining) on success
        """
        if len(buffer) < 4:
            return None, buffer
        
        length = int.from_bytes(buffer[:4], 'big')
        
        # Security: Reject messages larger than MAX_MESSAGE_SIZE
        if length > MAX_MESSAGE_SIZE:
            # Skip this malformed/malicious message, try to recover
            # Look for next valid message start (heuristic)
            return None, buffer[4:]  # Skip length bytes, try again
        
        if len(buffer) < 4 + length:
            return None, buffer
        
        try:
            data = buffer[4:4+length].decode('utf-8')
            message = json.loads(data)
            return message, buffer[4+length:]
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Skip corrupted message
            return None, buffer[4+length:]
    
    @staticmethod
    def encode_udp(message: dict) -> bytes:
        """Encode a message for UDP (no length prefix needed, single datagram)"""
        import zlib
        data = json.dumps(message, separators=(',', ':')).encode('utf-8')
        # Compress for faster transmission
        compressed = zlib.compress(data, level=1)
        return compressed
    
    @staticmethod
    def decode_udp(data: bytes) -> Optional[dict]:
        """Decode a UDP message"""
        import zlib
        try:
            decompressed = zlib.decompress(data)
            return json.loads(decompressed.decode('utf-8'))
        except (zlib.error, json.JSONDecodeError, UnicodeDecodeError):
            return None


class GameServer:
    """TCP/UDP Game Server - handles all game logic and client connections
    
    Uses TCP for reliable messages (join, start, restart, inputs)
    Uses UDP for fast game state broadcasts
    
    Security features:
    - Password authentication for non-private IPs
    - Connection limiting
    - Rate limiting per client
    - Input validation
    - Max message size enforcement
    """
    
    def __init__(self, host: str, port: int, mode: str, speed: str, 
                 walls_enabled: bool, width: int, height: int, password: str = None):
        self.host = host
        self.port = port
        self.udp_port = port + 1  # UDP on next port
        self.mode = mode
        self.speed = speed
        self.walls_enabled = walls_enabled
        self.width = width
        self.height = height
        self.tick_rate = SPEED_SETTINGS.get(speed, 0.15)
        
        # Security: Password for non-private IPs
        self.requires_password = not is_private_ip(host)
        if self.requires_password:
            self.password = password if password else generate_password()
        else:
            self.password = None
        
        self.server_socket = None
        self.udp_socket = None  # UDP socket for broadcasting
        self.clients: Dict[str, dict] = {}  # player_id -> {socket, buffer, name, ...}
        self.game: Optional[GameData] = None
        self.running = False
        self.logic: Optional[SnakeGameLogic] = None
        
        # Pending inputs from clients
        self.pending_inputs: Dict[str, str] = {}
        self.inputs_lock = threading.Lock()
        
        # UDP client addresses for broadcasting
        self.udp_clients: Dict[str, tuple] = {}  # player_id -> (ip, port)
        self.udp_lock = threading.Lock()
        
        # Security: Rate limiting - track message counts per client
        self.client_message_counts: Dict[str, list] = {}  # player_id -> [timestamps]
        self.rate_limit_lock = threading.Lock()
        
        # Security: Connection counting
        self.total_connections = 0
    
    def start(self):
        """Start the game server"""
        # TCP socket for reliable messages
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(MAX_CONNECTIONS)
        self.server_socket.setblocking(False)
        
        # UDP socket for fast game state broadcasts
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_socket.bind((self.host, self.udp_port))
        self.udp_socket.setblocking(False)
        
        print(f"Game server started on {self.host}:{self.port} (TCP) and :{self.udp_port} (UDP)")
        print(f"Mode: {self.mode}, Speed: {self.speed}, Walls: {self.walls_enabled}")
        
        # Security: Show password if required
        if self.requires_password:
            print("=" * 60)
            print("⚠️  SERVER IS POTENTIALLY INTERNET-EXPOSED!")
            print(f"🔑 PASSWORD: {self.password}")
            print("   Share this password with players to allow them to join.")
            print("=" * 60)
        else:
            print("(Private network detected - no password required)")
        
        print("Waiting for players to connect...")
        print("Press Ctrl+C to stop the server")
        
        # Initialize game
        self.logic = SnakeGameLogic(
            player_id="server",
            player_name="Server",
            is_host=True,
            mode=self.mode,
            speed=self.speed,
            walls_enabled=self.walls_enabled,
            width=self.width,
            height=self.height
        )
        
        self.game = GameData(
            state=GameState.WAITING.value,
            mode=self.mode,
            speed=self.speed,
            walls_enabled=self.walls_enabled,
            width=self.width,
            height=self.height,
            host_id="server",
            next_weapon_spawn=time.time() + random.uniform(WEAPON_SPAWN_MIN, WEAPON_SPAWN_MAX)
        )
        
        self.running = True
        self._run_loop()
    
    def _run_loop(self):
        """Main server loop"""
        last_update = time.time()
        
        try:
            while self.running:
                current_time = time.time()
                
                # Accept new connections
                self._accept_connections()
                
                # Read from clients
                self._read_clients()
                
                # Update game state
                if current_time - last_update >= self.tick_rate:
                    self._process_inputs()
                    
                    if self.game.state == GameState.COUNTDOWN.value:
                        self._update_countdown()
                    elif self.game.state == GameState.RUNNING.value:
                        self._update_game()
                    
                    # Broadcast state to all clients
                    self._broadcast_state()
                    last_update = current_time
                
                # Small sleep to prevent CPU spinning
                time.sleep(0.001)
                
        except KeyboardInterrupt:
            print("\nServer shutting down...")
        finally:
            self._cleanup()
    
    def _accept_connections(self):
        """Accept new client connections with connection limit"""
        try:
            readable, _, _ = select.select([self.server_socket], [], [], 0)
            if self.server_socket in readable:
                client_socket, addr = self.server_socket.accept()
                print(f"New connection from {addr} (total clients: {len(self.clients)})")
                
                # Security: Enforce connection limit
                self.total_connections = len(self.clients)
                if self.total_connections >= MAX_CONNECTIONS:
                    print(f"Connection rejected from {addr}: max connections ({MAX_CONNECTIONS}) reached")
                    try:
                        # Send error and close
                        error_msg = NetworkProtocol.encode({'type': 'error', 'message': 'Server full'})
                        client_socket.sendall(error_msg)
                        client_socket.close()
                    except:
                        pass
                    return
                
                client_socket.setblocking(False)
                print(f"New connection from {addr}")
                
                # Temporary ID until client sends join message
                temp_id = f"temp_{addr[0]}_{addr[1]}"
                self.clients[temp_id] = {
                    'socket': client_socket,
                    'buffer': b'',
                    'name': 'Unknown',
                    'addr': addr,
                    'joined': False,
                    'authenticated': False  # Security: Track auth status
                }
        except (BlockingIOError, socket.error):
            pass
    
    def _read_clients(self):
        """Read data from all connected clients"""
        disconnected = []
        
        for player_id, client in list(self.clients.items()):
            try:
                readable, _, _ = select.select([client['socket']], [], [], 0)
                if client['socket'] in readable:
                    data = client['socket'].recv(BUFFER_SIZE)
                    if not data:
                        try:
                            peer = client['socket'].getpeername()
                            debug_print(f"Client {player_id[:12]} (peer={peer}) sent empty data (disconnect)")
                        except:
                            debug_print(f"Client {player_id[:12]} sent empty data (disconnect, peer unknown)")
                        disconnected.append(player_id)
                        continue
                    
                    debug_print(f"SERVER: Received {len(data)} bytes from {player_id[:12]}")
                    client['buffer'] += data
                    
                    # Process complete messages
                    while True:
                        msg, client['buffer'] = NetworkProtocol.decode_from_buffer(client['buffer'])
                        if msg is None:
                            break
                        try:
                            self._handle_message(player_id, msg)
                        except Exception as e:
                            # Don't disconnect on message handling errors
                            print(f"Error handling message from {player_id}: {e}")
                        
            except (BlockingIOError, socket.error):
                pass
            except Exception as e:
                print(f"Error reading from {player_id}: {e}")
                disconnected.append(player_id)
        
        # Remove disconnected clients
        for player_id in disconnected:
            self._remove_client(player_id)
    
    def _check_rate_limit(self, client_id: str) -> bool:
        """Check if client exceeds rate limit. Returns True if allowed, False if rate limited."""
        current_time = time.time()
        
        with self.rate_limit_lock:
            if client_id not in self.client_message_counts:
                self.client_message_counts[client_id] = []
            
            # Remove timestamps older than 1 second
            self.client_message_counts[client_id] = [
                ts for ts in self.client_message_counts[client_id] 
                if current_time - ts < 1.0
            ]
            
            # Check if under limit
            if len(self.client_message_counts[client_id]) >= MAX_MESSAGES_PER_SECOND:
                return False
            
            # Add current timestamp
            self.client_message_counts[client_id].append(current_time)
            return True
    
    def _handle_message(self, temp_id: str, msg: dict):
        """Handle a message from a client with security checks"""
        # Security: Rate limiting
        if not self._check_rate_limit(temp_id):
            print(f"Rate limit exceeded for {temp_id}")
            return
        
        # Security: Validate message type
        msg_type = msg.get('type')
        if not isinstance(msg_type, str) or len(msg_type) > 20:
            return
        
        if msg_type == 'join':
            self._handle_join(temp_id, msg)
        elif msg_type == 'input':
            # Security: Only process if client is authenticated
            client = self.clients.get(temp_id)
            if not client or (self.requires_password and not client.get('authenticated')):
                return
            
            player_id = msg.get('player_id', temp_id)
            action = msg.get('action')
            
            # Security: Validate action
            if not isinstance(action, str) or action not in VALID_ACTIONS:
                return
            
            # Security: Prevent player impersonation - only allow own player_id
            if player_id != temp_id and player_id not in self.clients:
                return
            
            if player_id in self.game.snakes:
                with self.inputs_lock:
                    self.pending_inputs[player_id] = action
        elif msg_type == 'start':
            if self.game.state == GameState.WAITING.value and len(self.game.snakes) > 0:
                self._start_countdown()
        elif msg_type == 'restart':
            if self.game.state == GameState.FINISHED.value:
                self._restart_game()
    
    def _handle_join(self, temp_id: str, msg: dict):
        """Handle a join request with security validation"""
        # Security: Validate password if required
        if self.requires_password:
            client_password = msg.get('password', '')
            
            # Validate password format and content
            if not isinstance(client_password, str) or len(client_password) > 50:
                self._send_to_client(temp_id, {
                    'type': 'error',
                    'message': 'Invalid password format'
                })
                # Graceful disconnect to ensure error message is received
                self._remove_client(temp_id, graceful=True)
                return
            
            if client_password != self.password:
                self._send_to_client(temp_id, {
                    'type': 'error',
                    'message': 'Invalid password'
                })
                print(f"Authentication failed for {temp_id}")
                # Graceful disconnect to ensure error message is received
                self._remove_client(temp_id, graceful=True)
                return
            
            # Mark as authenticated
            if temp_id in self.clients:
                self.clients[temp_id]['authenticated'] = True
        
        if self.game.state != GameState.WAITING.value:
            self._send_to_client(temp_id, {
                'type': 'error',
                'message': 'Game already started'
            })
            return
        
        if len(self.game.snakes) >= MAX_PLAYERS:
            self._send_to_client(temp_id, {
                'type': 'error',
                'message': 'Game is full'
            })
            return
        
        # Security: Validate and sanitize player name
        raw_name = msg.get('name', 'Player')
        if not isinstance(raw_name, str):
            raw_name = 'Player'
        # Remove control characters and limit length
        player_name = ''.join(c for c in raw_name if c.isprintable())[:16].strip()
        if not player_name:
            player_name = 'Player'
        
        # Security: Generate server-side player_id, don't trust client
        player_id = temp_id  # Use the temp_id we assigned
        
        # Security: Validate UDP port (0-65535)
        udp_port = msg.get('udp_port', 0)
        if not isinstance(udp_port, int) or udp_port < 0 or udp_port > 65535:
            udp_port = 0
        
        # Security: Validate screen dimensions
        client_width = msg.get('screen_width', 0)
        client_height = msg.get('screen_height', 0)
        if not isinstance(client_width, int) or client_width < 0 or client_width > 10000:
            client_width = 0
        if not isinstance(client_height, int) or client_height < 0 or client_height > 10000:
            client_height = 0
        
        # First player sets the game field size (capped at 1920x1080 equivalent)
        if len(self.game.snakes) == 0 and client_width > 0 and client_height > 0:
            # Cap at 1920x1080 for GUI, scale down for game grid
            max_width = min(client_width, 1920)
            max_height = min(client_height, 1080)
            # For terminal: use as-is (columns x rows)
            # For GUI: divide by cell size (assumed ~16 pixels)
            if client_width <= 300:  # Terminal mode (columns)
                self.game.width = min(client_width - 2, 200)
                self.game.height = min(client_height - 3, 50)
            else:  # GUI mode (pixels)
                cell_size = 16
                self.game.width = min(max_width // cell_size - 2, 118)  # ~1920/16 - 2
                self.game.height = min(max_height // cell_size - 4, 63)  # ~1080/16 - 4
            
            self.width = self.game.width
            self.height = self.game.height
            print(f"Game field size set by first player: {self.game.width}x{self.game.height}")
        
        # Add player to game
        self.logic._add_player_to_game(self.game, player_id, player_name)
        
        # Update client entry (player_id == temp_id, so just update in place)
        if temp_id in self.clients:
            self.clients[temp_id]['name'] = player_name
            self.clients[temp_id]['joined'] = True
            self.clients[temp_id]['authenticated'] = True
        
        # Security: Use client IP from TCP connection, not from client message
        # This prevents UDP amplification attacks with spoofed IPs
        if udp_port > 0:
            try:
                client_ip = self.clients[temp_id]['socket'].getpeername()[0]
                with self.udp_lock:
                    self.udp_clients[player_id] = (client_ip, udp_port)
                print(f"Player '{player_name}' joined (ID: {player_id[:8]}..., UDP: {client_ip}:{udp_port})")
            except Exception as e:
                print(f"Player '{player_name}' joined (ID: {player_id[:8]}..., UDP setup failed: {e})")
        else:
            print(f"Player '{player_name}' joined (ID: {player_id[:8]}..., no UDP)")
        
        # Send welcome message with player_id, server's UDP port, and game dimensions
        debug_print(f"SERVER: Sending welcome message to {player_id[:12]}...")
        success = self._send_to_client(player_id, {
            'type': 'welcome',
            'player_id': player_id,
            'udp_port': self.udp_port,
            'game_width': self.game.width,
            'game_height': self.game.height,
            'message': f'Welcome {player_name}!'
        })
        debug_print(f"SERVER: Welcome message sent, success={success}")
        
        # Broadcast updated state
        debug_print(f"SERVER: Broadcasting state...")
        self._broadcast_state()
        debug_print(f"SERVER: State broadcast complete")
    
    def _start_countdown(self):
        """Start the game countdown"""
        self.game.state = GameState.COUNTDOWN.value
        self.game.countdown = 5
        self.game.countdown_start = time.time()
        print("Game starting in 5 seconds...")
    
    def _restart_game(self):
        """Restart the game with all connected players"""
        print("Restarting game...")
        
        # Save connected player info
        player_info = {}
        for player_id, client in self.clients.items():
            if client.get('joined'):
                player_info[player_id] = client.get('name', 'Player')
        
        # Reset game state
        self.game = GameData()
        self.game.mode = self.mode
        self.game.no_walls = self.no_walls
        self.game.state = GameState.WAITING.value
        
        # Re-add all connected players
        for player_id, name in player_info.items():
            self.logic._add_player_to_game(self.game, player_id, name)
            print(f"Player '{name}' re-added to game")
        
        # Clear pending inputs
        with self.inputs_lock:
            self.pending_inputs.clear()
        
        # Start countdown if players are present
        if len(self.game.snakes) > 0:
            self._start_countdown()
        
        print(f"Game restarted with {len(player_info)} players")
    
    def _update_countdown(self):
        """Update countdown timer"""
        elapsed = time.time() - self.game.countdown_start
        new_countdown = 5 - int(elapsed)
        
        if new_countdown != self.game.countdown:
            self.game.countdown = max(0, new_countdown)
            if self.game.countdown > 0:
                print(f"Starting in {self.game.countdown}...")
        
        if elapsed >= 5.0:
            self.game.state = GameState.RUNNING.value
            self.game.countdown = 0
            print("Game started!")
    
    def _process_inputs(self):
        """Process pending player inputs"""
        with self.inputs_lock:
            inputs = self.pending_inputs.copy()
            self.pending_inputs.clear()
        
        current_time = time.time()
        
        for player_id, action in inputs.items():
            if player_id not in self.game.snakes:
                continue
            
            snake = self.game.snakes[player_id]
            if not snake['alive']:
                continue
            
            current_dir = Direction(snake['direction'])
            
            if action == 'UP' and current_dir != Direction.DOWN:
                snake['direction'] = Direction.UP.value
            elif action == 'DOWN' and current_dir != Direction.UP:
                snake['direction'] = Direction.DOWN.value
            elif action == 'LEFT' and current_dir != Direction.RIGHT:
                snake['direction'] = Direction.LEFT.value
            elif action == 'RIGHT' and current_dir != Direction.LEFT:
                snake['direction'] = Direction.RIGHT.value
            elif action == 'FIRE' and snake.get('has_weapon'):
                self.logic._fire_weapon(self.game, snake)
            elif action == 'GHOST' and snake.get('has_ghost'):
                self.logic._activate_ghost(self.game, snake, current_time)
    
    def _update_game(self):
        """Update game state"""
        current_time = time.time()
        
        # Update invisibility status
        for player_id, snake in self.game.snakes.items():
            if snake.get('is_invisible') and current_time >= snake.get('invisible_until', 0):
                snake['is_invisible'] = False
        
        # Move snakes
        for player_id, snake in self.game.snakes.items():
            if not snake['alive']:
                continue
            self.logic._move_snake(self.game, snake)
        
        # Update bombs
        self.logic._update_bombs(self.game)
        
        # Update explosions
        self.logic._update_explosions(self.game)
        
        # Check collisions
        self.logic._check_collisions(self.game)
        
        # Spawn weapons
        if current_time >= self.game.next_weapon_spawn:
            if random.random() < 0.5:
                self.logic._spawn_weapon(self.game)
            else:
                self.logic._spawn_ghost(self.game)
            self.game.next_weapon_spawn = current_time + random.uniform(WEAPON_SPAWN_MIN, WEAPON_SPAWN_MAX)
        
        # Check winner
        alive_count = sum(1 for s in self.game.snakes.values() if s['alive'])
        if alive_count <= 1 and len(self.game.snakes) > 1:
            self.game.state = GameState.FINISHED.value
            for pid, s in self.game.snakes.items():
                if s['alive']:
                    self.game.winner = s['player_name']
                    print(f"Game over! Winner: {self.game.winner}")
        
        self.game.tick += 1
    
    def _broadcast_state(self):
        """Send game state to all clients via UDP (fast) with TCP fallback"""
        state_msg = {
            'type': 'state',
            'game': asdict(self.game)
        }
        
        # Use UDP for fast broadcasting (may be blocked by firewall)
        try:
            data = NetworkProtocol.encode_udp(state_msg)
            
            with self.udp_lock:
                for player_id, udp_addr in list(self.udp_clients.items()):
                    try:
                        self.udp_socket.sendto(data, udp_addr)
                        debug_print(f"SERVER: Sent {len(data)} bytes UDP to {udp_addr}")
                    except Exception as e:
                        debug_print(f"SERVER: UDP send error to {udp_addr}: {e}")
        except Exception as e:
            debug_print(f"SERVER: UDP broadcast error: {e}")
        
        # Also send via TCP as fallback (in case UDP is blocked by firewall)
        # Only send every Nth tick to reduce TCP overhead, or always during WAITING state
        should_send_tcp = (self.game.tick % 3 == 0) or (self.game.state == GameState.WAITING.value)
        if should_send_tcp:
            disconnected = []
            for player_id, client in list(self.clients.items()):
                if client.get('joined'):
                    if not self._send_to_client(player_id, state_msg):
                        debug_print(f"Failed to send state to {player_id[:12]}, marking for disconnect")
                        disconnected.append(player_id)
            
            for player_id in disconnected:
                self._remove_client(player_id)
    
    def _send_to_client(self, player_id: str, msg: dict) -> bool:
        """Send a message to a specific client. Returns True on success, False on failure."""
        if player_id not in self.clients:
            return False
        
        try:
            data = NetworkProtocol.encode(msg)
            self.clients[player_id]['socket'].sendall(data)
            return True
        except Exception as e:
            print(f"Error sending to {player_id}: {e}")
            return False
    
    def _remove_client(self, player_id: str, graceful: bool = False):
        """Remove a client from the game
        
        Args:
            player_id: The client's player ID
            graceful: If True, do a graceful shutdown to allow pending data to be sent
        """
        if player_id in self.clients:
            client = self.clients[player_id]
            print(f"Player '{client['name']}' disconnected (graceful={graceful})")
            try:
                if graceful:
                    # Graceful shutdown: allow pending data to be sent
                    client['socket'].shutdown(socket.SHUT_WR)
                    time.sleep(0.1)  # Brief delay to allow data transmission
                client['socket'].close()
            except:
                pass
            del self.clients[player_id]
        
        # Remove from UDP clients list
        with self.udp_lock:
            if player_id in self.udp_clients:
                del self.udp_clients[player_id]
        
        if player_id in self.game.snakes:
            # Mark snake as dead instead of removing
            self.game.snakes[player_id]['alive'] = False
    
    def _cleanup(self):
        """Clean up server resources"""
        self.running = False
        
        for client in self.clients.values():
            try:
                client['socket'].close()
            except:
                pass
        
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        
        if self.udp_socket:
            try:
                self.udp_socket.close()
            except:
                pass
        
        print("Server stopped.")


class GameClient:
    """TCP/UDP Game Client - uses TCP for reliable inputs, UDP for fast state updates"""
    
    def __init__(self, host: str, port: int, player_name: str, screen_width: int = 0, 
                 screen_height: int = 0, password: str = None):
        self.host = host
        self.port = port
        self.player_name = player_name
        self.player_id = get_user_id()
        self.screen_width = screen_width  # Client's screen size
        self.screen_height = screen_height
        self.password = password  # Server password (if required)
        
        self.socket = None  # TCP socket for reliable messages
        self.udp_socket = None  # UDP socket for receiving game state
        self.buffer = b''
        self.game: Optional[GameData] = None
        self.connected = False
        self.running = False
        self.error_message = ""
        self.udp_port = 0  # Local UDP port for receiving broadcasts
        self.server_udp_port = 0  # Server's UDP port
        self.welcome_message = ""  # Store welcome message
        
        # UDP statistics for debugging
        self.udp_packets_received = 0
        self.tcp_packets_received = 0
        self.last_udp_time = 0
        self.last_stats_print = 0
    
    def connect(self) -> bool:
        """Connect to the game server"""
        try:
            # Create UDP socket first to get a port number
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_socket.bind(('0.0.0.0', 0))  # Bind to any available port
            self.udp_socket.setblocking(False)
            self.udp_port = self.udp_socket.getsockname()[1]
            
            # TCP connection
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(5.0)  # 5 second timeout for connect
            self.socket.connect((self.host, self.port))
            self.socket.setblocking(False)
            
            # Send join message with our UDP port, screen size, and password
            join_msg = {
                'type': 'join',
                'name': self.player_name,
                'player_id': self.player_id,
                'udp_port': self.udp_port,
                'screen_width': self.screen_width,
                'screen_height': self.screen_height
            }
            
            # Include password if provided
            if self.password:
                join_msg['password'] = self.password
            
            data = NetworkProtocol.encode(join_msg)
            self.socket.sendall(data)
            
            self.connected = True
            return True
            
        except socket.timeout:
            self.error_message = f"Connection timeout - server at {self.host}:{self.port} not responding"
            return False
        except ConnectionRefusedError:
            self.error_message = f"Connection refused - no server at {self.host}:{self.port}"
            return False
        except Exception as e:
            self.error_message = f"Connection error: {e}"
            return False
    
    def send_input(self, action: str):
        """Send an input action to the server"""
        if not self.connected:
            return
        
        msg = {
            'type': 'input',
            'player_id': self.player_id,
            'action': action
        }
        
        try:
            data = NetworkProtocol.encode(msg)
            self.socket.sendall(data)
        except Exception as e:
            print(f"Error sending input: {e}")
            self.connected = False
    
    def send_start(self):
        """Send start game request"""
        if not self.connected:
            return
        
        msg = {'type': 'start'}
        try:
            data = NetworkProtocol.encode(msg)
            self.socket.sendall(data)
        except:
            pass
    
    def send_restart(self):
        """Send restart game request"""
        if not self.connected:
            return
        
        msg = {'type': 'restart'}
        try:
            data = NetworkProtocol.encode(msg)
            self.socket.sendall(data)
        except:
            pass
    
    def receive_state(self) -> Optional[GameData]:
        """Receive and process messages from server (UDP for state, TCP for control messages)"""
        if not self.connected:
            return None
        
        current_time = time.time()
        
        # First, receive UDP game state updates (fast path)
        try:
            while True:
                try:
                    data, addr = self.udp_socket.recvfrom(UDP_BUFFER_SIZE)
                    msg = NetworkProtocol.decode_udp(data)
                    if msg and msg.get('type') == 'state':
                        game_data = msg.get('game')
                        if game_data:
                            self.game = GameData(**game_data)
                            self.udp_packets_received += 1
                            self.last_udp_time = current_time
                except BlockingIOError:
                    break  # No more UDP data
                except Exception as e:
                    debug_print(f"CLIENT: UDP receive error: {e}")
                    break
        except Exception as e:
            debug_print(f"CLIENT: UDP outer error: {e}")
        
        # Also check TCP for control messages (welcome, error, etc.)
        try:
            readable, _, _ = select.select([self.socket], [], [], 0)
            if self.socket in readable:
                data = self.socket.recv(BUFFER_SIZE)
                if not data:
                    debug_print(f"CLIENT: Received empty data from server (connection closed)")
                    self.connected = False
                    self.error_message = "Server disconnected"
                    return None
                
                debug_print(f"CLIENT: Received {len(data)} bytes via TCP")
                self.buffer += data
                
                # Process all complete messages
                while True:
                    msg, self.buffer = NetworkProtocol.decode_from_buffer(self.buffer)
                    if msg is None:
                        break
                    
                    if msg.get('type') == 'welcome':
                        # Update player_id to the one assigned by server
                        server_player_id = msg.get('player_id')
                        if server_player_id:
                            self.player_id = server_player_id
                            debug_print(f"CLIENT: Server assigned player_id: {server_player_id[:12]}...")
                        self.server_udp_port = msg.get('udp_port', 0)
                        self.welcome_message = msg.get('message', '')
                        debug_print(f"CLIENT: Welcome received. Server UDP port: {self.server_udp_port}, our UDP port: {self.udp_port}")
                    elif msg.get('type') == 'error':
                        self.error_message = msg.get('message', 'Unknown error')
                        print(f"Server error: {self.error_message}")
                    elif msg.get('type') == 'state':
                        # Fallback: also accept state via TCP
                        self.tcp_packets_received += 1
                        game_data = msg.get('game')
                        if game_data:
                            try:
                                self.game = GameData(**game_data)
                            except Exception as e:
                                print(f"Error parsing game state: {e}")
                        
        except BlockingIOError:
            pass
        except Exception as e:
            self.error_message = f"Network error: {e}"
            self.connected = False
        
        # Print UDP vs TCP statistics periodically (every 5 seconds)
        if current_time - self.last_stats_print >= 5.0:
            self.last_stats_print = current_time
            udp_working = self.udp_packets_received > 0
            time_since_udp = current_time - self.last_udp_time if self.last_udp_time > 0 else float('inf')
            
            if not udp_working and self.tcp_packets_received > 0:
                debug_print(f"CLIENT: ⚠️ UDP not working! Using TCP fallback. "
                           f"TCP packets: {self.tcp_packets_received}, UDP packets: {self.udp_packets_received}")
                debug_print(f"CLIENT: Check if UDP port {self.server_udp_port} is open on server and not blocked by firewall")
            elif udp_working:
                debug_print(f"CLIENT: Network stats - UDP: {self.udp_packets_received}, TCP: {self.tcp_packets_received}, "
                           f"last UDP: {time_since_udp:.1f}s ago")
        return self.game
    
    def disconnect(self):
        """Disconnect from server"""
        self.connected = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        if self.udp_socket:
            try:
                self.udp_socket.close()
            except:
                pass


class NetworkTerminalClient:
    """Terminal client that connects to a game server via TCP/UDP"""
    
    def __init__(self, stdscr, host: str, port: int, player_name: str, password: str = None):
        self.stdscr = stdscr
        self.height, self.width = stdscr.getmaxyx()
        
        # Pass screen size and password to client (width=columns, height=rows)
        self.client = GameClient(host, port, player_name, 
                                  screen_width=self.width, 
                                  screen_height=self.height,
                                  password=password)
        self.running = True
        self.render_buffer = {}
        
        curses.curs_set(0)
        self.stdscr.nodelay(1)
        self.stdscr.timeout(50)
        
        curses.start_color()
        curses.use_default_colors()
        for i, color in enumerate(PLAYER_COLORS):
            curses.init_pair(i + 1, color, -1)
        curses.init_pair(11, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(12, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(13, curses.COLOR_RED, -1)
        curses.init_pair(14, curses.COLOR_WHITE, -1)
        curses.init_pair(15, curses.COLOR_YELLOW, curses.COLOR_RED)
    
    def render(self, game: GameData):
        """Render the game state"""
        new_buffer = {}
        
        # Draw border (only if walls enabled)
        if game.walls_enabled:
            for x in range(min(game.width + 1, self.width - 1)):
                new_buffer[(x, 0)] = (WALL_SYMBOL, curses.color_pair(14))
                if game.height < self.height:
                    new_buffer[(x, game.height)] = (WALL_SYMBOL, curses.color_pair(14))
            
            for y in range(min(game.height + 1, self.height - 1)):
                new_buffer[(0, y)] = (WALL_SYMBOL, curses.color_pair(14))
                if game.width < self.width:
                    new_buffer[(game.width, y)] = (WALL_SYMBOL, curses.color_pair(14))
        
        # Draw explosions
        for exp in game.explosions:
            x, y = exp['x'], exp['y']
            for dx in range(-exp['radius'], exp['radius'] + 1):
                for dy in range(-exp['radius'], exp['radius'] + 1):
                    if dx*dx + dy*dy <= exp['radius']*exp['radius']:
                        px, py = x + dx, y + dy
                        if 0 < py < self.height - 1 and 0 < px < self.width - 1:
                            new_buffer[(px, py)] = (EXPLOSION_SYMBOL, curses.color_pair(15) | curses.A_BOLD)
        
        # Draw foods
        for food in game.foods:
            x, y = food[0], food[1]
            if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                new_buffer[(x, y)] = (FOOD_SYMBOL, curses.color_pair(11))
        
        # Draw weapons
        for weapon in game.weapons:
            x, y = weapon[0], weapon[1]
            if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                new_buffer[(x, y)] = (WEAPON_SYMBOL, curses.color_pair(12))
        
        # Draw ghost pickups
        for ghost in game.ghost_pickups:
            x, y = ghost[0], ghost[1]
            if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                new_buffer[(x, y)] = (GHOST_SYMBOL, curses.color_pair(12) | curses.A_BOLD)
        
        # Draw bombs
        for bomb in game.bombs:
            x, y = bomb['x'], bomb['y']
            if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                new_buffer[(x, y)] = (BOMB_SYMBOL, curses.color_pair(13) | curses.A_BOLD)
        
        # Draw snakes
        for player_id, snake in game.snakes.items():
            color = curses.color_pair(snake['color'] + 1)
            is_me = player_id == self.client.player_id
            is_invisible = snake.get('is_invisible', False)
            
            # Skip invisible snakes that aren't ours
            if is_invisible and not is_me:
                continue
            
            for i, segment in enumerate(snake['body']):
                x, y = segment[0], segment[1]
                if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                    if not snake['alive']:
                        char = DEAD_SNAKE
                        color_to_use = curses.color_pair(14)
                    elif i == 0:
                        char = SNAKE_HEAD
                        color_to_use = color | (curses.A_BOLD if is_me else 0)
                    else:
                        char = SNAKE_BODY
                        color_to_use = color
                    
                    new_buffer[(x, y)] = (char, color_to_use)
        
        # Draw player names above snake heads (only during WAITING state)
        if game.state == GameState.WAITING.value:
            for player_id, snake in game.snakes.items():
                if snake['body']:
                    head = snake['body'][0]
                    name = snake['player_name'][:10]  # Limit name length
                    color = curses.color_pair(snake['color'] + 1)
                    name_y = head[1] - 1  # One row above head
                    name_x = head[0] - len(name) // 2  # Center above head
                    
                    # Draw name character by character
                    for i, ch in enumerate(name):
                        nx = name_x + i
                        if 0 < name_y < self.height - 1 and 0 < nx < self.width - 1:
                            new_buffer[(nx, name_y)] = (ch, color | curses.A_BOLD)
        
        # Update display
        for pos in self.render_buffer:
            if pos not in new_buffer:
                x, y = pos
                if 0 <= y < self.height and 0 <= x < self.width:
                    try:
                        self.stdscr.addch(y, x, ' ')
                    except:
                        pass
        
        for pos, (char, attr) in new_buffer.items():
            if pos not in self.render_buffer or self.render_buffer[pos] != (char, attr):
                x, y = pos
                if 0 <= y < self.height and 0 <= x < self.width:
                    try:
                        self.stdscr.addch(y, x, char, attr)
                    except:
                        pass
        
        self.render_buffer = new_buffer
        
        # Status bar
        status_y = min(game.height + 1, self.height - 2)
        if status_y < self.height:
            try:
                self.stdscr.move(status_y, 0)
                self.stdscr.clrtoeol()
                if status_y + 1 < self.height:
                    self.stdscr.move(status_y + 1, 0)
                    self.stdscr.clrtoeol()
            except:
                pass
            
            if game.state == GameState.WAITING.value:
                player_count = len(game.snakes)
                walls_str = "WALLS" if game.walls_enabled else "NO-WALLS"
                status = f" WAITING - {player_count} player(s) | Mode: {game.mode.upper()} | Speed: {game.speed.upper()} | {walls_str}"
                status += " | Press 'S' to START"
            elif game.state == GameState.COUNTDOWN.value:
                status = f" >>> STARTING IN {game.countdown} <<< "
            elif game.state == GameState.RUNNING.value:
                my_snake = game.snakes.get(self.client.player_id, {})
                weapon_status = ""
                if my_snake.get('has_weapon'):
                    weapon_status += " [BOMB:SPACE]"
                if my_snake.get('has_ghost'):
                    weapon_status += " [GHOST:G]"
                if my_snake.get('is_invisible'):
                    weapon_status += " [INVISIBLE!]"
                alive_status = "ALIVE" if my_snake.get('alive', False) else "DEAD (spectating)"
                score = my_snake.get('score', 0)
                status = f" Score: {score} | {alive_status}{weapon_status} | Speed: {game.speed.upper()}"
            else:
                status = f" GAME OVER! Winner: {game.winner} | Press 'R' to RESTART or 'Q' to quit"
            
            try:
                self.stdscr.addstr(status_y, 0, status[:self.width-1])
            except:
                pass
        
        if status_y + 1 < self.height:
            players_str = " Players: "
            for pid, snake in game.snakes.items():
                name = snake['player_name'][:6]
                status_char = "+" if snake['alive'] else "-"
                weapon_char = ""
                if snake.get('has_weapon'):
                    weapon_char += "[B]"
                if snake.get('has_ghost'):
                    weapon_char += "[G]"
                if snake.get('is_invisible'):
                    weapon_char += "[!]"
                players_str += f"{status_char}{name}({snake['score']}){weapon_char} "
            try:
                self.stdscr.addstr(status_y + 1, 0, players_str[:self.width-1])
            except:
                pass
        
        self.stdscr.refresh()
    
    def handle_input(self) -> Optional[str]:
        """Handle keyboard input"""
        try:
            key = self.stdscr.getch()
        except:
            return None
        
        if key == ord('q') or key == ord('Q'):
            self.running = False
            return 'QUIT'
        
        if key == curses.KEY_UP:
            return 'UP'
        elif key == curses.KEY_DOWN:
            return 'DOWN'
        elif key == curses.KEY_LEFT:
            return 'LEFT'
        elif key == curses.KEY_RIGHT:
            return 'RIGHT'
        elif key == ord(' '):
            return 'FIRE'
        elif key == ord('g') or key == ord('G'):
            return 'GHOST'
        elif key == ord('s') or key == ord('S'):
            return 'START'
        elif key == ord('r') or key == ord('R'):
            return 'RESTART'
        
        return None
    
    def run(self):
        """Run the network terminal client"""
        self.stdscr.clear()
        self.stdscr.addstr(5, 5, f"Connecting to {self.client.host}:{self.client.port}...")
        self.stdscr.refresh()
        
        if not self.client.connect():
            self.stdscr.clear()
            self.stdscr.addstr(5, 5, f"Connection failed: {self.client.error_message}")
            self.stdscr.addstr(7, 5, "Press any key to exit...")
            self.stdscr.nodelay(0)
            self.stdscr.getch()
            return
        
        self.stdscr.clear()
        self.render_buffer = {}
        
        while self.running and self.client.connected:
            action = self.handle_input()
            if action == 'QUIT':
                break
            elif action == 'START':
                self.client.send_start()
            elif action == 'RESTART':
                self.client.send_restart()
            elif action in ['UP', 'DOWN', 'LEFT', 'RIGHT', 'FIRE', 'GHOST']:
                game = self.client.game
                if game and self.client.player_id in game.snakes:
                    if game.snakes[self.client.player_id].get('alive', False):
                        self.client.send_input(action)
            
            game = self.client.receive_state()
            if game:
                self.render(game)
            
            time.sleep(0.02)
        
        if not self.client.connected and self.client.error_message:
            self.stdscr.clear()
            self.stdscr.addstr(5, 5, f"Disconnected: {self.client.error_message}")
            self.stdscr.addstr(7, 5, "Press any key to exit...")
            self.stdscr.nodelay(0)
            self.stdscr.getch()
        
        self.client.disconnect()


class TerminalGame:
    """Terminal-based game rendering using curses"""
    
    def __init__(self, stdscr, logic: SnakeGameLogic):
        self.stdscr = stdscr
        self.logic = logic
        self.running = True
        self.render_buffer = {}
        
        curses.curs_set(0)
        self.stdscr.nodelay(1)
        self.stdscr.timeout(50)
        
        curses.start_color()
        curses.use_default_colors()
        for i, color in enumerate(PLAYER_COLORS):
            curses.init_pair(i + 1, color, -1)
        curses.init_pair(11, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(12, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(13, curses.COLOR_RED, -1)
        curses.init_pair(14, curses.COLOR_WHITE, -1)
        curses.init_pair(15, curses.COLOR_YELLOW, curses.COLOR_RED)
        
        self.height, self.width = stdscr.getmaxyx()
        self.logic.height = self.height - 3
        self.logic.width = self.width - 2
    
    def render(self, game: GameData):
        """Render the game state"""
        new_buffer = {}
        
        # Draw border (only if walls enabled)
        if game.walls_enabled:
            for x in range(min(game.width + 1, self.width - 1)):
                new_buffer[(x, 0)] = (WALL_SYMBOL, curses.color_pair(14))
                if game.height < self.height:
                    new_buffer[(x, game.height)] = (WALL_SYMBOL, curses.color_pair(14))
            
            for y in range(min(game.height + 1, self.height - 1)):
                new_buffer[(0, y)] = (WALL_SYMBOL, curses.color_pair(14))
                if game.width < self.width:
                    new_buffer[(game.width, y)] = (WALL_SYMBOL, curses.color_pair(14))
        
        # Draw explosions
        for exp in game.explosions:
            x, y = exp['x'], exp['y']
            for dx in range(-exp['radius'], exp['radius'] + 1):
                for dy in range(-exp['radius'], exp['radius'] + 1):
                    if dx*dx + dy*dy <= exp['radius']*exp['radius']:
                        px, py = x + dx, y + dy
                        if 0 < py < self.height - 1 and 0 < px < self.width - 1:
                            new_buffer[(px, py)] = (EXPLOSION_SYMBOL, curses.color_pair(15) | curses.A_BOLD)
        
        # Draw foods
        for food in game.foods:
            x, y = food[0], food[1]
            if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                new_buffer[(x, y)] = (FOOD_SYMBOL, curses.color_pair(11))
        
        # Draw weapons
        for weapon in game.weapons:
            x, y = weapon[0], weapon[1]
            if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                new_buffer[(x, y)] = (WEAPON_SYMBOL, curses.color_pair(12))
        
        # Draw ghost pickups
        for ghost in game.ghost_pickups:
            x, y = ghost[0], ghost[1]
            if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                new_buffer[(x, y)] = (GHOST_SYMBOL, curses.color_pair(12) | curses.A_BOLD)
        
        # Draw bombs
        for bomb in game.bombs:
            x, y = bomb['x'], bomb['y']
            if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                new_buffer[(x, y)] = (BOMB_SYMBOL, curses.color_pair(13) | curses.A_BOLD)
        
        # Draw snakes
        for player_id, snake in game.snakes.items():
            color = curses.color_pair(snake['color'] + 1)
            is_me = player_id == self.logic.player_id
            
            for i, segment in enumerate(snake['body']):
                x, y = segment[0], segment[1]
                if 0 < y < self.height - 1 and 0 < x < self.width - 1:
                    if not snake['alive']:
                        char = DEAD_SNAKE
                        color_to_use = curses.color_pair(14)
                    elif i == 0:
                        char = SNAKE_HEAD
                        color_to_use = color | (curses.A_BOLD if is_me else 0)
                    else:
                        char = SNAKE_BODY
                        color_to_use = color
                    
                    new_buffer[(x, y)] = (char, color_to_use)
        
        # Update display
        for pos in self.render_buffer:
            if pos not in new_buffer:
                x, y = pos
                if 0 <= y < self.height and 0 <= x < self.width:
                    try:
                        self.stdscr.addch(y, x, ' ')
                    except:
                        pass
        
        for pos, (char, attr) in new_buffer.items():
            if pos not in self.render_buffer or self.render_buffer[pos] != (char, attr):
                x, y = pos
                if 0 <= y < self.height and 0 <= x < self.width:
                    try:
                        self.stdscr.addch(y, x, char, attr)
                    except:
                        pass
        
        self.render_buffer = new_buffer
        
        # Status bar
        status_y = min(game.height + 1, self.height - 2)
        if status_y < self.height:
            try:
                self.stdscr.move(status_y, 0)
                self.stdscr.clrtoeol()
                if status_y + 1 < self.height:
                    self.stdscr.move(status_y + 1, 0)
                    self.stdscr.clrtoeol()
            except:
                pass
            
            if game.state == GameState.WAITING.value:
                player_count = len(game.snakes)
                walls_str = "WALLS" if game.walls_enabled else "NO-WALLS"
                status = f" WAITING - {player_count} player(s) | Mode: {game.mode.upper()} | Speed: {game.speed.upper()} | {walls_str}"
                if self.logic.is_host:
                    status += " | Press 'S' to START"
            elif game.state == GameState.COUNTDOWN.value:
                status = f" >>> STARTING IN {game.countdown} <<< "
            elif game.state == GameState.RUNNING.value:
                my_snake = game.snakes.get(self.logic.player_id, {})
                weapon_status = ""
                if my_snake.get('has_weapon'):
                    weapon_status += " [BOMB:SPACE]"
                if my_snake.get('has_ghost'):
                    weapon_status += " [GHOST:G]"
                if my_snake.get('is_invisible'):
                    weapon_status += " [INVISIBLE!]"
                alive_status = "ALIVE" if my_snake.get('alive', False) else "DEAD (spectating)"
                score = my_snake.get('score', 0)
                status = f" Score: {score} | {alive_status}{weapon_status} | Speed: {game.speed.upper()}"
            else:
                status = f" GAME OVER! Winner: {game.winner} | Press 'Q' to quit"
            
            try:
                self.stdscr.addstr(status_y, 0, status[:self.width-1])
            except:
                pass
        
        if status_y + 1 < self.height:
            players_str = " Players: "
            for pid, snake in game.snakes.items():
                name = snake['player_name'][:6]
                status_char = "+" if snake['alive'] else "-"
                weapon_char = ""
                if snake.get('has_weapon'):
                    weapon_char += "[B]"
                if snake.get('has_ghost'):
                    weapon_char += "[G]"
                if snake.get('is_invisible'):
                    weapon_char += "[!]"
                players_str += f"{status_char}{name}({snake['score']}){weapon_char} "
            try:
                self.stdscr.addstr(status_y + 1, 0, players_str[:self.width-1])
            except:
                pass
        
        self.stdscr.refresh()
    
    def handle_input(self) -> Optional[str]:
        """Handle keyboard input"""
        try:
            key = self.stdscr.getch()
        except:
            return None
        
        if key == ord('q') or key == ord('Q'):
            self.running = False
            return 'QUIT'
        
        if key == curses.KEY_UP:
            return 'UP'
        elif key == curses.KEY_DOWN:
            return 'DOWN'
        elif key == curses.KEY_LEFT:
            return 'LEFT'
        elif key == curses.KEY_RIGHT:
            return 'RIGHT'
        elif key == ord(' '):
            return 'FIRE'
        elif key == ord('g') or key == ord('G'):
            return 'GHOST'
        elif key == ord('s') or key == ord('S'):
            return 'START'
        
        return None
    
    def run_host(self):
        """Run as host"""
        game = self.logic.init_host_game()
        last_update = time.time()
        
        self.stdscr.clear()
        self.render_buffer = {}
        
        while self.running:
            current_time = time.time()
            
            action = self.handle_input()
            if action == 'QUIT':
                break
            elif action == 'START' and game.state == GameState.WAITING.value:
                self.logic.start_game(game)
            elif action in ['UP', 'DOWN', 'LEFT', 'RIGHT', 'FIRE', 'GHOST']:
                send_input(self.logic.player_id, action)
            
            tick_rate = SPEED_SETTINGS.get(game.speed, 0.15)
            
            # In WAITING state, check for new players joining
            if game.state == GameState.WAITING.value:
                with FileLock(LOCK_FILE):
                    file_game = load_game_state()
                    if file_game and file_game.state == GameState.WAITING.value:
                        # Merge new players from file into our game state
                        for pid, snake_data in file_game.snakes.items():
                            if pid not in game.snakes:
                                game.snakes[pid] = snake_data
                                game.next_player_id = max(game.next_player_id, snake_data['player_id'] + 1)
                        save_game_state(game)
            
            # Handle countdown
            if game.state == GameState.COUNTDOWN.value:
                self.logic.update_countdown(game)
            
            if current_time - last_update >= tick_rate:
                with FileLock(LOCK_FILE):
                    game = load_game_state() or game
                    if game.state == GameState.COUNTDOWN.value:
                        self.logic.update_countdown(game)
                    elif game.state == GameState.RUNNING.value:
                        self.logic.update_game(game)
                    else:
                        save_game_state(game)
                last_update = current_time
            else:
                with FileLock(LOCK_FILE):
                    game = load_game_state() or game
            
            self.render(game)
            time.sleep(0.02)
        
        if self.logic.is_host:
            try:
                import shutil
                shutil.rmtree(GAME_DIR, ignore_errors=True)
            except:
                pass
    
    def run_client(self):
        """Run as client"""
        game = self.logic.join_game()
        
        if game is None:
            self.stdscr.addstr(5, 5, "No game found or game already started!")
            self.stdscr.addstr(6, 5, "Make sure a host has started a game.")
            self.stdscr.addstr(7, 5, "Press any key to exit...")
            self.stdscr.nodelay(0)
            self.stdscr.getch()
            return
        
        self.stdscr.clear()
        self.render_buffer = {}
        
        while self.running:
            action = self.handle_input()
            if action == 'QUIT':
                break
            elif action in ['UP', 'DOWN', 'LEFT', 'RIGHT', 'FIRE', 'GHOST']:
                # Only send input if we're alive
                if game and self.logic.player_id in game.snakes:
                    if game.snakes[self.logic.player_id].get('alive', False):
                        send_input(self.logic.player_id, action)
            
            game = load_game_state()
            if game is None:
                break
            
            self.render(game)
            time.sleep(0.02)


class GUIGame:
    """Pygame-based GUI game rendering"""
    
    def __init__(self, logic: SnakeGameLogic, fullscreen: bool = True):
        if not PYGAME_AVAILABLE:
            raise RuntimeError("Pygame is not installed. Install with: pip install pygame")
        
        self.logic = logic
        self.running = True
        
        pygame.init()
        pygame.display.set_caption("Snake Multiplayer")
        
        # Get display info
        display_info = pygame.display.Info()
        
        if fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            self.screen_width = display_info.current_w
            self.screen_height = display_info.current_h
        else:
            self.screen_width = 1280
            self.screen_height = 720
            self.screen = pygame.display.set_mode((self.screen_width, self.screen_height))
        
        # Calculate cell size
        self.cell_size = min(self.screen_width // 80, self.screen_height // 50)
        self.logic.width = self.screen_width // self.cell_size - 2
        self.logic.height = self.screen_height // self.cell_size - 4
        
        # Load/create sprites
        self._create_sprites()
        
        # Fonts
        self.font = pygame.font.Font(None, 36)
        self.small_font = pygame.font.Font(None, 24)
        
        # Explosion particles
        self.particles = []
        
        # Clock
        self.clock = pygame.time.Clock()
    
    def _create_sprites(self):
        """Create game sprites"""
        cs = self.cell_size
        
        # Snake head sprite (circle with eyes)
        self.head_sprites = {}
        for i, color in enumerate(GUI_COLORS):
            surf = pygame.Surface((cs, cs), pygame.SRCALPHA)
            pygame.draw.circle(surf, color, (cs//2, cs//2), cs//2 - 1)
            # Eyes
            pygame.draw.circle(surf, (255, 255, 255), (cs//3, cs//3), cs//6)
            pygame.draw.circle(surf, (0, 0, 0), (cs//3, cs//3), cs//10)
            pygame.draw.circle(surf, (255, 255, 255), (2*cs//3, cs//3), cs//6)
            pygame.draw.circle(surf, (0, 0, 0), (2*cs//3, cs//3), cs//10)
            self.head_sprites[i] = surf
        
        # Snake body sprite
        self.body_sprites = {}
        for i, color in enumerate(GUI_COLORS):
            surf = pygame.Surface((cs, cs), pygame.SRCALPHA)
            pygame.draw.rect(surf, color, (2, 2, cs-4, cs-4), border_radius=3)
            self.body_sprites[i] = surf
        
        # Dead snake sprite
        self.dead_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        pygame.draw.rect(self.dead_sprite, (100, 100, 100), (2, 2, cs-4, cs-4), border_radius=3)
        pygame.draw.line(self.dead_sprite, (50, 50, 50), (3, 3), (cs-3, cs-3), 2)
        pygame.draw.line(self.dead_sprite, (50, 50, 50), (cs-3, 3), (3, cs-3), 2)
        
        # Food sprite (apple-like)
        self.food_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        pygame.draw.circle(self.food_sprite, (255, 50, 50), (cs//2, cs//2 + 2), cs//2 - 2)
        pygame.draw.circle(self.food_sprite, (255, 100, 100), (cs//3, cs//3), cs//6)
        pygame.draw.rect(self.food_sprite, (100, 50, 0), (cs//2-1, 2, 3, cs//4))
        pygame.draw.ellipse(self.food_sprite, (50, 200, 50), (cs//2, 0, cs//3, cs//4))
        
        # Weapon sprite (bomb)
        self.weapon_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        pygame.draw.circle(self.weapon_sprite, (50, 50, 50), (cs//2, cs//2 + 2), cs//2 - 2)
        pygame.draw.circle(self.weapon_sprite, (80, 80, 80), (cs//3, cs//3), cs//6)
        pygame.draw.rect(self.weapon_sprite, (150, 100, 50), (cs//2-2, 0, 4, cs//3))
        # Fuse
        pygame.draw.arc(self.weapon_sprite, (255, 200, 0), (cs//3, -cs//4, cs//3, cs//2), 0, 3.14, 2)
        
        # Flying bomb sprite
        self.bomb_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        pygame.draw.circle(self.bomb_sprite, (255, 100, 0), (cs//2, cs//2), cs//2 - 1)
        pygame.draw.circle(self.bomb_sprite, (255, 255, 0), (cs//2, cs//2), cs//3)
        
        # Ghost pickup sprite (small ghost)
        self.ghost_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        # Ghost body (rounded top, wavy bottom)
        pygame.draw.ellipse(self.ghost_sprite, (200, 200, 255), (2, 2, cs-4, cs*2//3))
        pygame.draw.rect(self.ghost_sprite, (200, 200, 255), (2, cs//3, cs-4, cs//3))
        # Wavy bottom
        for i in range(3):
            wave_x = 2 + i * (cs-4) // 3
            pygame.draw.ellipse(self.ghost_sprite, (200, 200, 255), 
                              (wave_x, cs*2//3 - 2, (cs-4)//3, cs//4))
        # Eyes
        pygame.draw.ellipse(self.ghost_sprite, (0, 0, 0), (cs//4, cs//4, cs//5, cs//4))
        pygame.draw.ellipse(self.ghost_sprite, (0, 0, 0), (cs//2 + cs//8, cs//4, cs//5, cs//4))
        
        # Invisible snake sprites (semi-transparent, only visible to owner)
        self.invisible_head_sprites = {}
        self.invisible_body_sprites = {}
        for i, color in enumerate(GUI_COLORS):
            # Semi-transparent head
            surf = pygame.Surface((cs, cs), pygame.SRCALPHA)
            ghost_color = (*color, 80)  # Alpha = 80
            pygame.draw.circle(surf, ghost_color, (cs//2, cs//2), cs//2 - 1)
            self.invisible_head_sprites[i] = surf
            
            # Semi-transparent body
            surf = pygame.Surface((cs, cs), pygame.SRCALPHA)
            pygame.draw.rect(surf, ghost_color, (2, 2, cs-4, cs-4), border_radius=3)
            self.invisible_body_sprites[i] = surf
        
        # Wall sprite
        self.wall_sprite = pygame.Surface((cs, cs))
        self.wall_sprite.fill((80, 80, 80))
        pygame.draw.rect(self.wall_sprite, (60, 60, 60), (0, 0, cs, cs), 2)
    
    def _create_explosion_particles(self, x: int, y: int):
        """Create explosion particle effect"""
        cs = self.cell_size
        center_x = x * cs + cs // 2
        center_y = y * cs + cs // 2
        
        for _ in range(30):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(2, 8)
            self.particles.append({
                'x': center_x,
                'y': center_y,
                'vx': math.cos(angle) * speed,
                'vy': math.sin(angle) * speed,
                'life': random.randint(20, 40),
                'color': random.choice([(255, 200, 0), (255, 100, 0), (255, 50, 0), (255, 255, 100)])
            })
    
    def _update_particles(self):
        """Update explosion particles"""
        for p in self.particles[:]:
            p['x'] += p['vx']
            p['y'] += p['vy']
            p['vy'] += 0.2  # Gravity
            p['life'] -= 1
            if p['life'] <= 0:
                self.particles.remove(p)
    
    def render(self, game: GameData):
        """Render the game"""
        cs = self.cell_size
        
        # Background
        self.screen.fill((20, 20, 40))
        
        # Draw walls if enabled
        if game.walls_enabled:
            for x in range(game.width + 1):
                self.screen.blit(self.wall_sprite, (x * cs, 0))
                self.screen.blit(self.wall_sprite, (x * cs, game.height * cs))
            for y in range(game.height + 1):
                self.screen.blit(self.wall_sprite, (0, y * cs))
                self.screen.blit(self.wall_sprite, (game.width * cs, y * cs))
        
        # Draw foods
        for food in game.foods:
            self.screen.blit(self.food_sprite, (food[0] * cs, food[1] * cs))
        
        # Draw bomb weapons
        for weapon in game.weapons:
            self.screen.blit(self.weapon_sprite, (weapon[0] * cs, weapon[1] * cs))
        
        # Draw ghost pickups
        for ghost in game.ghost_pickups:
            self.screen.blit(self.ghost_sprite, (ghost[0] * cs, ghost[1] * cs))
        
        # Draw snakes
        for player_id, snake in game.snakes.items():
            color_idx = snake['color'] % len(GUI_COLORS)
            is_me = player_id == self.logic.player_id
            is_invisible = snake.get('is_invisible', False)
            
            # Skip invisible snakes that aren't ours
            if is_invisible and not is_me:
                continue
            
            for i, segment in enumerate(snake['body']):
                x, y = segment[0] * cs, segment[1] * cs
                
                if not snake['alive']:
                    self.screen.blit(self.dead_sprite, (x, y))
                elif i == 0:
                    # Rotate head based on direction
                    if is_invisible:
                        head = self.invisible_head_sprites[color_idx].copy()
                    else:
                        head = self.head_sprites[color_idx].copy()
                    direction = Direction(snake['direction'])
                    if direction == Direction.UP:
                        head = pygame.transform.rotate(head, 180)
                    elif direction == Direction.DOWN:
                        head = pygame.transform.rotate(head, 0)
                    elif direction == Direction.LEFT:
                        head = pygame.transform.rotate(head, -90)
                    elif direction == Direction.RIGHT:
                        head = pygame.transform.rotate(head, 90)
                    self.screen.blit(head, (x, y))
                else:
                    if is_invisible:
                        self.screen.blit(self.invisible_body_sprites[color_idx], (x, y))
                    else:
                        self.screen.blit(self.body_sprites[color_idx], (x, y))
        
        # Draw bombs
        for bomb in game.bombs:
            self.screen.blit(self.bomb_sprite, (bomb['x'] * cs, bomb['y'] * cs))
        
        # Draw explosion effects and create particles
        for exp in game.explosions:
            if exp['ttl'] == 4:  # New explosion
                self._create_explosion_particles(exp['x'], exp['y'])
            
            # Draw explosion circle
            center_x = exp['x'] * cs + cs // 2
            center_y = exp['y'] * cs + cs // 2
            radius = exp['radius'] * cs * (exp['ttl'] / 5)
            alpha = int(255 * exp['ttl'] / 5)
            
            exp_surf = pygame.Surface((radius*2, radius*2), pygame.SRCALPHA)
            pygame.draw.circle(exp_surf, (255, 200, 0, alpha), (radius, radius), radius)
            pygame.draw.circle(exp_surf, (255, 100, 0, alpha//2), (radius, radius), radius * 0.7)
            self.screen.blit(exp_surf, (center_x - radius, center_y - radius))
        
        # Update and draw particles
        self._update_particles()
        for p in self.particles:
            size = max(2, p['life'] // 5)
            pygame.draw.circle(self.screen, p['color'], (int(p['x']), int(p['y'])), size)
        
        # Draw status bar
        status_y = (game.height + 1) * cs + 10
        
        if game.state == GameState.WAITING.value:
            walls_str = "WALLS" if game.walls_enabled else "NO-WALLS"
            text = f"WAITING - {len(game.snakes)} player(s) | Mode: {game.mode.upper()} | Speed: {game.speed.upper()} | {walls_str}"
            if self.logic.is_host:
                text += " | Press 'S' to START"
            text_surf = self.font.render(text, True, (255, 255, 255))
            self.screen.blit(text_surf, (10, status_y))
        elif game.state == GameState.COUNTDOWN.value:
            # Draw big countdown in center of screen
            countdown_text = str(game.countdown) if game.countdown > 0 else "GO!"
            countdown_font = pygame.font.Font(None, 200)
            countdown_surf = countdown_font.render(countdown_text, True, (255, 255, 0))
            countdown_rect = countdown_surf.get_rect(center=(self.screen_width // 2, self.screen_height // 2))
            self.screen.blit(countdown_surf, countdown_rect)
            
            # Also show status
            text = f"GET READY! Starting in {game.countdown}..."
            text_surf = self.font.render(text, True, (255, 255, 255))
            self.screen.blit(text_surf, (10, status_y))
        elif game.state == GameState.RUNNING.value:
            my_snake = game.snakes.get(self.logic.player_id, {})
            weapon_text = ""
            if my_snake.get('has_weapon'):
                weapon_text += " [SPACE: BOMB]"
            if my_snake.get('has_ghost'):
                weapon_text += " [G: GHOST]"
            if my_snake.get('is_invisible'):
                weapon_text += " [INVISIBLE!]"
            status = "ALIVE" if my_snake.get('alive', False) else "DEAD (spectating)"
            text = f"Score: {my_snake.get('score', 0)} | {status}{weapon_text}"
            text_surf = self.font.render(text, True, (255, 255, 255))
            self.screen.blit(text_surf, (10, status_y))
        else:
            text = f"GAME OVER! Winner: {game.winner} | Press 'Q' to quit"
            text_surf = self.font.render(text, True, (255, 255, 0))
            self.screen.blit(text_surf, (10, status_y))
        
        # Player list
        player_x = 10
        for pid, snake in game.snakes.items():
            color = GUI_COLORS[snake['color'] % len(GUI_COLORS)]
            status = "●" if snake['alive'] else "○"
            weapons = ""
            if snake.get('has_weapon'):
                weapons += "[B]"
            if snake.get('has_ghost'):
                weapons += "[G]"
            if snake.get('is_invisible'):
                weapons += "[👻]"
            text = f"{status} {snake['player_name']}: {snake['score']}{weapons}"
            text_surf = self.small_font.render(text, True, color)
            self.screen.blit(text_surf, (player_x, status_y + 30))
            player_x += text_surf.get_width() + 20
        
        pygame.display.flip()
    
    def handle_input(self) -> Optional[str]:
        """Handle pygame input"""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
                return 'QUIT'
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE or event.key == pygame.K_q:
                    self.running = False
                    return 'QUIT'
                elif event.key == pygame.K_UP:
                    return 'UP'
                elif event.key == pygame.K_DOWN:
                    return 'DOWN'
                elif event.key == pygame.K_LEFT:
                    return 'LEFT'
                elif event.key == pygame.K_RIGHT:
                    return 'RIGHT'
                elif event.key == pygame.K_SPACE:
                    return 'FIRE'
                elif event.key == pygame.K_g:
                    return 'GHOST'
                elif event.key == pygame.K_s:
                    return 'START'
        return None
    
    def run_host(self):
        """Run as host - optimized for single player or local hosting"""
        game = self.logic.init_host_game()
        last_update = time.time()
        last_debug = time.time()
        tick_count = 0
        
        # Store tick rate from game speed setting
        tick_rate = SPEED_SETTINGS.get(self.logic.speed, 0.15)
        debug_print(f"Starting with speed={self.logic.speed}, tick_rate={tick_rate}")
        
        # For direct input handling (faster than file-based)
        pending_action = None
        
        while self.running:
            current_time = time.time()
            
            action = self.handle_input()
            if action == 'QUIT':
                break
            elif action == 'START' and game.state == GameState.WAITING.value:
                self.logic.start_game(game)
            elif action in ['UP', 'DOWN', 'LEFT', 'RIGHT', 'FIRE', 'GHOST']:
                pending_action = action
            
            # In WAITING state, periodically check for new players joining
            if game.state == GameState.WAITING.value:
                with FileLock(LOCK_FILE):
                    file_game = load_game_state()
                    if file_game and file_game.state == GameState.WAITING.value:
                        # Merge new players from file into our game state
                        for pid, snake_data in file_game.snakes.items():
                            if pid not in game.snakes:
                                game.snakes[pid] = snake_data
                                game.next_player_id = max(game.next_player_id, snake_data['player_id'] + 1)
                        save_game_state(game)
            
            # Handle countdown
            if game.state == GameState.COUNTDOWN.value:
                self.logic.update_countdown(game)
            
            if current_time - last_update >= tick_rate:
                # Apply pending action directly for host (much faster)
                if pending_action and self.logic.player_id in game.snakes:
                    snake = game.snakes[self.logic.player_id]
                    if snake['alive']:
                        current_dir = Direction(snake['direction'])
                        if pending_action == 'UP' and current_dir != Direction.DOWN:
                            snake['direction'] = Direction.UP.value
                        elif pending_action == 'DOWN' and current_dir != Direction.UP:
                            snake['direction'] = Direction.DOWN.value
                        elif pending_action == 'LEFT' and current_dir != Direction.RIGHT:
                            snake['direction'] = Direction.LEFT.value
                        elif pending_action == 'RIGHT' and current_dir != Direction.LEFT:
                            snake['direction'] = Direction.RIGHT.value
                        elif pending_action == 'FIRE' and snake.get('has_weapon'):
                            self.logic._fire_weapon(game, snake)
                        elif pending_action == 'GHOST' and snake.get('has_ghost'):
                            self.logic._activate_ghost(game, snake, current_time)
                    pending_action = None
                
                # Read and process inputs from other players (file-based)
                other_inputs = read_inputs()
                for player_id, input_action in other_inputs.items():
                    if player_id in game.snakes and player_id != self.logic.player_id:
                        snake = game.snakes[player_id]
                        if snake['alive']:
                            current_dir = Direction(snake['direction'])
                            if input_action == 'UP' and current_dir != Direction.DOWN:
                                snake['direction'] = Direction.UP.value
                            elif input_action == 'DOWN' and current_dir != Direction.UP:
                                snake['direction'] = Direction.DOWN.value
                            elif input_action == 'LEFT' and current_dir != Direction.RIGHT:
                                snake['direction'] = Direction.LEFT.value
                            elif input_action == 'RIGHT' and current_dir != Direction.LEFT:
                                snake['direction'] = Direction.RIGHT.value
                            elif input_action == 'FIRE' and snake.get('has_weapon'):
                                self.logic._fire_weapon(game, snake)
                            elif input_action == 'GHOST' and snake.get('has_ghost'):
                                self.logic._activate_ghost(game, snake, current_time)
                
                # Update invisibility status
                for player_id, snake in game.snakes.items():
                    if snake.get('is_invisible') and current_time >= snake.get('invisible_until', 0):
                        snake['is_invisible'] = False
                
                if game.state == GameState.RUNNING.value:
                    # Move snakes
                    for player_id, snake in game.snakes.items():
                        if not snake['alive']:
                            continue
                        self.logic._move_snake(game, snake)
                    
                    # Update bombs
                    self.logic._update_bombs(game)
                    
                    # Update explosions
                    self.logic._update_explosions(game)
                    
                    # Check collisions
                    self.logic._check_collisions(game)
                    
                    # Spawn weapons (alternating between bomb and ghost)
                    if current_time >= game.next_weapon_spawn:
                        if random.random() < 0.5:
                            self.logic._spawn_weapon(game)
                        else:
                            self.logic._spawn_ghost(game)
                        game.next_weapon_spawn = current_time + random.uniform(WEAPON_SPAWN_MIN, WEAPON_SPAWN_MAX)
                    
                    # Check winner
                    alive_count = sum(1 for s in game.snakes.values() if s['alive'])
                    if alive_count <= 1 and len(game.snakes) > 1:
                        game.state = GameState.FINISHED.value
                        for pid, s in game.snakes.items():
                            if s['alive']:
                                game.winner = s['player_name']
                    
                    game.tick += 1
                    tick_count += 1
                
                # Debug: Show ticks per second
                if current_time - last_debug >= 1.0:
                    debug_print(f"{tick_count} ticks in last second (target: {1.0/tick_rate:.1f})")
                    tick_count = 0
                    last_debug = current_time
                
                # Save game state for other players to sync (every tick for multiplayer)
                try:
                    with FileLock(LOCK_FILE):
                        save_game_state(game)
                except:
                    pass
                
                last_update = current_time
            
            self.render(game)
            # Higher FPS for faster game speeds
            target_fps = max(60, int(1.0 / tick_rate) + 10)
            self.clock.tick(target_fps)
        
        pygame.quit()
        
        if self.logic.is_host:
            try:
                import shutil
                shutil.rmtree(GAME_DIR, ignore_errors=True)
            except:
                pass
    
    def run_client(self):
        """Run as client"""
        game = self.logic.join_game()
        
        if game is None:
            # Show error in pygame
            self.screen.fill((0, 0, 0))
            text = self.font.render("No game found or game already started!", True, (255, 0, 0))
            self.screen.blit(text, (100, 100))
            text2 = self.font.render("Press any key to exit...", True, (255, 255, 255))
            self.screen.blit(text2, (100, 150))
            pygame.display.flip()
            
            waiting = True
            while waiting:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT or event.type == pygame.KEYDOWN:
                        waiting = False
            pygame.quit()
            return
        
        while self.running:
            action = self.handle_input()
            if action == 'QUIT':
                break
            elif action in ['UP', 'DOWN', 'LEFT', 'RIGHT', 'FIRE', 'GHOST']:
                # Only send input if we're alive
                if game and self.logic.player_id in game.snakes:
                    if game.snakes[self.logic.player_id].get('alive', False):
                        send_input(self.logic.player_id, action)
            
            game = load_game_state()
            if game is None:
                break
            
            self.render(game)
            # Higher FPS for faster game speeds
            tick_rate = SPEED_SETTINGS.get(game.speed if game else 'normal', 0.15)
            target_fps = max(60, int(1.0 / tick_rate) + 10)
            self.clock.tick(target_fps)
        
        pygame.quit()


class NetworkGUIClient:
    """GUI client that connects to a game server via TCP"""
    
    def __init__(self, host: str, port: int, player_name: str, fullscreen: bool = True, password: str = None):
        if not PYGAME_AVAILABLE:
            raise RuntimeError("Pygame is not installed. Install with: pip install pygame")
        
        pygame.init()
        pygame.display.set_caption(f"Snake Multiplayer - Connecting to {host}:{port}")
        
        display_info = pygame.display.Info()
        
        if fullscreen:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
            self.screen_width = display_info.current_w
            self.screen_height = display_info.current_h
        else:
            self.screen_width = 1280
            self.screen_height = 720
            self.screen = pygame.display.set_mode((self.screen_width, self.screen_height))
        
        # Cap screen size for game field calculation (max 1920x1080)
        capped_width = min(self.screen_width, 1920)
        capped_height = min(self.screen_height, 1080)
        
        # Create client with screen size info and password
        self.client = GameClient(host, port, player_name, 
                                  screen_width=capped_width, 
                                  screen_height=capped_height,
                                  password=password)
        self.running = True
        
        self.cell_size = min(self.screen_width // 80, self.screen_height // 50)
        
        # Create sprites (reuse from GUIGame)
        self._create_sprites()
        
        self.font = pygame.font.Font(None, 36)
        self.small_font = pygame.font.Font(None, 24)
        self.name_font = pygame.font.Font(None, 28)  # Font for player names
        self.particles = []
        self.clock = pygame.time.Clock()
    
    def _create_sprites(self):
        """Create game sprites (same as GUIGame)"""
        cs = self.cell_size
        
        self.head_sprites = {}
        for i, color in enumerate(GUI_COLORS):
            surf = pygame.Surface((cs, cs), pygame.SRCALPHA)
            pygame.draw.circle(surf, color, (cs//2, cs//2), cs//2 - 1)
            pygame.draw.circle(surf, (255, 255, 255), (cs//3, cs//3), cs//6)
            pygame.draw.circle(surf, (0, 0, 0), (cs//3, cs//3), cs//10)
            pygame.draw.circle(surf, (255, 255, 255), (2*cs//3, cs//3), cs//6)
            pygame.draw.circle(surf, (0, 0, 0), (2*cs//3, cs//3), cs//10)
            self.head_sprites[i] = surf
        
        self.body_sprites = {}
        for i, color in enumerate(GUI_COLORS):
            surf = pygame.Surface((cs, cs), pygame.SRCALPHA)
            pygame.draw.rect(surf, color, (2, 2, cs-4, cs-4), border_radius=3)
            self.body_sprites[i] = surf
        
        self.dead_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        pygame.draw.rect(self.dead_sprite, (100, 100, 100), (2, 2, cs-4, cs-4), border_radius=3)
        pygame.draw.line(self.dead_sprite, (50, 50, 50), (3, 3), (cs-3, cs-3), 2)
        pygame.draw.line(self.dead_sprite, (50, 50, 50), (cs-3, 3), (3, cs-3), 2)
        
        self.food_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        pygame.draw.circle(self.food_sprite, (255, 50, 50), (cs//2, cs//2 + 2), cs//2 - 2)
        pygame.draw.circle(self.food_sprite, (255, 100, 100), (cs//3, cs//3), cs//6)
        pygame.draw.rect(self.food_sprite, (100, 50, 0), (cs//2-1, 2, 3, cs//4))
        pygame.draw.ellipse(self.food_sprite, (50, 200, 50), (cs//2, 0, cs//3, cs//4))
        
        self.weapon_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        pygame.draw.circle(self.weapon_sprite, (50, 50, 50), (cs//2, cs//2 + 2), cs//2 - 2)
        pygame.draw.circle(self.weapon_sprite, (80, 80, 80), (cs//3, cs//3), cs//6)
        pygame.draw.rect(self.weapon_sprite, (150, 100, 50), (cs//2-2, 0, 4, cs//3))
        
        self.bomb_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        pygame.draw.circle(self.bomb_sprite, (255, 100, 0), (cs//2, cs//2), cs//2 - 1)
        pygame.draw.circle(self.bomb_sprite, (255, 255, 0), (cs//2, cs//2), cs//3)
        
        self.ghost_sprite = pygame.Surface((cs, cs), pygame.SRCALPHA)
        pygame.draw.ellipse(self.ghost_sprite, (200, 200, 255), (2, 2, cs-4, cs*2//3))
        pygame.draw.rect(self.ghost_sprite, (200, 200, 255), (2, cs//3, cs-4, cs//3))
        pygame.draw.ellipse(self.ghost_sprite, (0, 0, 0), (cs//4, cs//4, cs//5, cs//4))
        pygame.draw.ellipse(self.ghost_sprite, (0, 0, 0), (cs//2 + cs//8, cs//4, cs//5, cs//4))
        
        self.invisible_head_sprites = {}
        self.invisible_body_sprites = {}
        for i, color in enumerate(GUI_COLORS):
            surf = pygame.Surface((cs, cs), pygame.SRCALPHA)
            ghost_color = (*color, 80)
            pygame.draw.circle(surf, ghost_color, (cs//2, cs//2), cs//2 - 1)
            self.invisible_head_sprites[i] = surf
            surf = pygame.Surface((cs, cs), pygame.SRCALPHA)
            pygame.draw.rect(surf, ghost_color, (2, 2, cs-4, cs-4), border_radius=3)
            self.invisible_body_sprites[i] = surf
        
        self.wall_sprite = pygame.Surface((cs, cs))
        self.wall_sprite.fill((80, 80, 80))
        pygame.draw.rect(self.wall_sprite, (60, 60, 60), (0, 0, cs, cs), 2)
    
    def render(self, game: GameData):
        """Render the game state"""
        cs = self.cell_size
        self.screen.fill((20, 20, 40))
        
        if game.walls_enabled:
            for x in range(game.width + 1):
                self.screen.blit(self.wall_sprite, (x * cs, 0))
                self.screen.blit(self.wall_sprite, (x * cs, game.height * cs))
            for y in range(game.height + 1):
                self.screen.blit(self.wall_sprite, (0, y * cs))
                self.screen.blit(self.wall_sprite, (game.width * cs, y * cs))
        
        for food in game.foods:
            self.screen.blit(self.food_sprite, (food[0] * cs, food[1] * cs))
        
        for weapon in game.weapons:
            self.screen.blit(self.weapon_sprite, (weapon[0] * cs, weapon[1] * cs))
        
        for ghost in game.ghost_pickups:
            self.screen.blit(self.ghost_sprite, (ghost[0] * cs, ghost[1] * cs))
        
        for player_id, snake in game.snakes.items():
            color_idx = snake['color'] % len(GUI_COLORS)
            is_me = player_id == self.client.player_id
            is_invisible = snake.get('is_invisible', False)
            
            if is_invisible and not is_me:
                continue
            
            for i, segment in enumerate(snake['body']):
                x, y = segment[0] * cs, segment[1] * cs
                if not snake['alive']:
                    self.screen.blit(self.dead_sprite, (x, y))
                elif i == 0:
                    if is_invisible:
                        head = self.invisible_head_sprites[color_idx].copy()
                    else:
                        head = self.head_sprites[color_idx].copy()
                    direction = Direction(snake['direction'])
                    if direction == Direction.UP:
                        head = pygame.transform.rotate(head, 180)
                    elif direction == Direction.LEFT:
                        head = pygame.transform.rotate(head, -90)
                    elif direction == Direction.RIGHT:
                        head = pygame.transform.rotate(head, 90)
                    self.screen.blit(head, (x, y))
                else:
                    if is_invisible:
                        self.screen.blit(self.invisible_body_sprites[color_idx], (x, y))
                    else:
                        self.screen.blit(self.body_sprites[color_idx], (x, y))
        
        for bomb in game.bombs:
            self.screen.blit(self.bomb_sprite, (bomb['x'] * cs, bomb['y'] * cs))
        
        # Draw player names above snake heads (only during WAITING state)
        if game.state == GameState.WAITING.value:
            for player_id, snake in game.snakes.items():
                if snake['body']:
                    head = snake['body'][0]
                    name = snake['player_name']
                    color = GUI_COLORS[snake['color'] % len(GUI_COLORS)]
                    
                    # Render name text
                    name_surf = self.name_font.render(name, True, color)
                    # Add outline/shadow for better visibility
                    shadow_surf = self.name_font.render(name, True, (0, 0, 0))
                    
                    # Position above head, centered
                    name_x = head[0] * cs + cs // 2 - name_surf.get_width() // 2
                    name_y = head[1] * cs - name_surf.get_height() - 5
                    
                    # Draw shadow then text
                    self.screen.blit(shadow_surf, (name_x + 1, name_y + 1))
                    self.screen.blit(name_surf, (name_x, name_y))
        
        # Status bar
        status_y = (game.height + 1) * cs + 10
        
        if game.state == GameState.WAITING.value:
            walls_str = "WALLS" if game.walls_enabled else "NO-WALLS"
            text = f"WAITING - {len(game.snakes)} player(s) | Mode: {game.mode.upper()} | Speed: {game.speed.upper()} | {walls_str}"
            text += " | Press 'S' to START"
            text_surf = self.font.render(text, True, (255, 255, 255))
            self.screen.blit(text_surf, (10, status_y))
        elif game.state == GameState.COUNTDOWN.value:
            countdown_text = str(game.countdown) if game.countdown > 0 else "GO!"
            countdown_font = pygame.font.Font(None, 200)
            countdown_surf = countdown_font.render(countdown_text, True, (255, 255, 0))
            countdown_rect = countdown_surf.get_rect(center=(self.screen_width // 2, self.screen_height // 2))
            self.screen.blit(countdown_surf, countdown_rect)
            text = f"GET READY! Starting in {game.countdown}..."
            text_surf = self.font.render(text, True, (255, 255, 255))
            self.screen.blit(text_surf, (10, status_y))
        elif game.state == GameState.RUNNING.value:
            my_snake = game.snakes.get(self.client.player_id, {})
            weapon_text = ""
            if my_snake.get('has_weapon'):
                weapon_text += " [SPACE: BOMB]"
            if my_snake.get('has_ghost'):
                weapon_text += " [G: GHOST]"
            if my_snake.get('is_invisible'):
                weapon_text += " [INVISIBLE!]"
            status = "ALIVE" if my_snake.get('alive', False) else "DEAD (spectating)"
            text = f"Score: {my_snake.get('score', 0)} | {status}{weapon_text}"
            text_surf = self.font.render(text, True, (255, 255, 255))
            self.screen.blit(text_surf, (10, status_y))
        else:
            text = f"GAME OVER! Winner: {game.winner} | Press 'R' to restart | Press 'Q' to quit"
            text_surf = self.font.render(text, True, (255, 255, 0))
            self.screen.blit(text_surf, (10, status_y))
        
        # Player list
        player_x = 10
        for pid, snake in game.snakes.items():
            color = GUI_COLORS[snake['color'] % len(GUI_COLORS)]
            status = "●" if snake['alive'] else "○"
            text = f"{status} {snake['player_name']}: {snake['score']}"
            text_surf = self.small_font.render(text, True, color)
            self.screen.blit(text_surf, (player_x, status_y + 30))
            player_x += text_surf.get_width() + 20
        
        pygame.display.flip()
    
    def handle_input(self) -> Optional[str]:
        """Handle pygame input"""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                debug_print(f"CLIENT: pygame.QUIT event received")
                self.running = False
                return 'QUIT'
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE or event.key == pygame.K_q:
                    debug_print(f"CLIENT: ESC/Q key pressed")
                    self.running = False
                    return 'QUIT'
                elif event.key == pygame.K_UP:
                    return 'UP'
                elif event.key == pygame.K_DOWN:
                    return 'DOWN'
                elif event.key == pygame.K_LEFT:
                    return 'LEFT'
                elif event.key == pygame.K_RIGHT:
                    return 'RIGHT'
                elif event.key == pygame.K_SPACE:
                    return 'FIRE'
                elif event.key == pygame.K_g:
                    return 'GHOST'
                elif event.key == pygame.K_s:
                    return 'START'
                elif event.key == pygame.K_r:
                    return 'RESTART'
        return None
    
    def run(self):
        """Run the network client"""
        try:
            # Show connecting screen
            self.screen.fill((0, 0, 0))
            text = self.font.render(f"Connecting to {self.client.host}:{self.client.port}...", True, (255, 255, 255))
            self.screen.blit(text, (100, 100))
            pygame.display.flip()
            
            if not self.client.connect():
                self.screen.fill((0, 0, 0))
                text = self.font.render(self.client.error_message, True, (255, 0, 0))
                self.screen.blit(text, (100, 100))
                text2 = self.font.render("Press any key to exit...", True, (255, 255, 255))
                self.screen.blit(text2, (100, 150))
                pygame.display.flip()
                
                waiting = True
                while waiting:
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT or event.type == pygame.KEYDOWN:
                            waiting = False
                pygame.quit()
                return
            
            debug_print(f"CLIENT: Connected successfully, entering main loop")
            debug_print(f"CLIENT: client.connected = {self.client.connected}")
            debug_print(f"CLIENT: self.running = {self.running}")
            
            # Clear any pending pygame events before entering main loop
            pending_events = pygame.event.get()
            if pending_events:
                debug_print(f"CLIENT: Cleared {len(pending_events)} pending events: {[e.type for e in pending_events]}")
            
            pygame.display.set_caption(f"Snake Multiplayer - Connected")
            
            # Show waiting for game state screen
            self.screen.fill((0, 0, 0))
            text = self.font.render("Connected! Waiting for game state...", True, (0, 255, 0))
            self.screen.blit(text, (100, 100))
            text2 = self.font.render(f"UDP Port: {self.client.udp_port} - If stuck, check firewall settings", True, (255, 255, 0))
            self.screen.blit(text2, (100, 150))
            pygame.display.flip()
            
            loop_count = 0
            while self.running and self.client.connected:
                loop_count += 1
                if loop_count <= 5:
                    debug_print(f"CLIENT: Loop iteration {loop_count}")
                
                action = self.handle_input()
                if action == 'QUIT':
                    debug_print(f"CLIENT: QUIT action received")
                    break
                elif action == 'START':
                    self.client.send_start()
                elif action == 'RESTART':
                    self.client.send_restart()
                elif action in ['UP', 'DOWN', 'LEFT', 'RIGHT', 'FIRE', 'GHOST']:
                    game = self.client.game
                    if game and self.client.player_id in game.snakes:
                        if game.snakes[self.client.player_id].get('alive', False):
                            self.client.send_input(action)
                
                game = self.client.receive_state()
                if game and loop_count <= 5:
                    debug_print(f"CLIENT: Received game state")
                
                if game:
                    self.render(game)
                
                self.clock.tick(60)
            
            debug_print(f"CLIENT: Exited main loop. running={self.running}, connected={self.client.connected}")
            
            if not self.client.connected and self.client.error_message:
                debug_print(f"CLIENT: Error: {self.client.error_message}")
                self.screen.fill((0, 0, 0))
                text = self.font.render(f"Disconnected: {self.client.error_message}", True, (255, 0, 0))
                self.screen.blit(text, (100, 100))
                text2 = self.font.render("Press any key to exit...", True, (255, 255, 255))
                self.screen.blit(text2, (100, 150))
                pygame.display.flip()
                
                waiting = True
                while waiting:
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT or event.type == pygame.KEYDOWN:
                            waiting = False
            
            self.client.disconnect()
            pygame.quit()
            
        except Exception as e:
            debug_print(f"CLIENT: EXCEPTION in run(): {e}")
            import traceback
            traceback.print_exc()
            self.client.disconnect()
            pygame.quit()


def run_terminal(args):
    """Run terminal version"""
    if IS_WINDOWS:
        print("Terminal mode is not supported on Windows.")
        print("Please use --gui flag for graphical mode.")
        sys.exit(1)
    
    def main(stdscr):
        player_id = get_user_id()
        logic = SnakeGameLogic(
            player_id=player_id,
            player_name=get_username(),
            is_host=args.host,
            mode=args.mode,
            speed=args.speed,
            walls_enabled=not args.no_walls,
            width=80,
            height=24
        )
        
        game = TerminalGame(stdscr, logic)
        
        if args.host:
            game.run_host()
        else:
            game.run_client()
    
    curses.wrapper(main)


def run_gui(args):
    """Run GUI version"""
    if not PYGAME_AVAILABLE:
        print("ERROR: Pygame is not installed!")
        print("Install with: pip install pygame")
        sys.exit(1)
    
    player_id = get_user_id()
    logic = SnakeGameLogic(
        player_id=player_id,
        player_name=get_username(),
        is_host=args.host,
        mode=args.mode,
        speed=args.speed,
        walls_enabled=not args.no_walls,
        width=80,
        height=40
    )
    
    game = GUIGame(logic, fullscreen=True)
    
    if args.host:
        game.run_host()
    else:
        game.run_client()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Multi-User Snake Game with TCP Networking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Start a dedicated game server:
    python3 snake_game.py --server --ip 0.0.0.0 --port 5555

  Connect to a server (GUI mode):
    python3 snake_game.py --connect 192.168.1.100:5555 --gui

  Connect to a server (Terminal mode):
    python3 snake_game.py --connect 192.168.1.100:5555

  Legacy file-based mode (local only):
    python3 snake_game.py --host --gui
    python3 snake_game.py --join --gui

Controls:
  Arrow keys: Move snake
  SPACE: Drop bomb (if you have one)
  G: Activate ghost mode (5 sec invisibility)
  S: Start game (in waiting state)
  R: Restart game (after game over)
  Q: Quit
"""
    )
    
    # Network mode (new)
    parser.add_argument('--server', action='store_true',
                        help='Start as dedicated game server (no GUI)')
    parser.add_argument('--connect', type=str, metavar='HOST:PORT',
                        help='Connect to a game server (e.g., 192.168.1.100:5555)')
    parser.add_argument('--ip', type=str, default='0.0.0.0',
                        help='IP address to bind server to (default: 0.0.0.0 = all interfaces)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'Port for server/client (default: {DEFAULT_PORT})')
    parser.add_argument('--name', type=str, default=None,
                        help='Player name (default: system username)')
    parser.add_argument('--password', type=str, default=None,
                        help='Server password (required for internet-exposed servers)')
    parser.add_argument('--windowed', action='store_true',
                        help='Run in windowed mode instead of fullscreen (for GUI mode)')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug output')
    
    # Legacy file-based mode
    parser.add_argument('--host', action='store_true',
                        help='Start as game host (legacy file-based mode)')
    parser.add_argument('--join', action='store_true',
                        help='Join an existing game (legacy file-based mode)')
    
    # Game options
    parser.add_argument('--mode', choices=['classic', 'kurve'], default='classic',
                        help='Game mode: classic or kurve (Achtung die Kurve)')
    parser.add_argument('--speed', choices=['normal', 'fast', 'ultra'], default='normal',
                        help='Game speed: normal, fast, or ultra')
    parser.add_argument('--no-walls', action='store_true',
                        help='Disable walls (snakes wrap around screen)')
    parser.add_argument('--gui', action='store_true',
                        help='Use graphical mode (requires pygame)')
    
    args = parser.parse_args()
    
    # Set debug mode
    set_debug_mode(args.debug)
    
    # Determine player name
    player_name = args.name if args.name else get_username()
    password = args.password  # May be None
    
    # === NETWORK SERVER MODE ===
    if args.server:
        print("=" * 60)
        print("SNAKE GAME - DEDICATED SERVER")
        print("=" * 60)
        # Default size is 16:9 ratio, will be adjusted by first client
        server = GameServer(
            host=args.ip,
            port=args.port,
            mode=args.mode,
            speed=args.speed,
            walls_enabled=not args.no_walls,
            width=118,   # ~1920/16 - 2 (max GUI width)
            height=63,   # ~1080/16 - 4 (max GUI height)
            password=password  # Use provided password or auto-generate
        )
        try:
            server.start()
        except KeyboardInterrupt:
            pass
        sys.exit(0)
    
    # === NETWORK CLIENT MODE ===
    if args.connect:
        # Parse host:port
        if ':' in args.connect:
            host, port_str = args.connect.rsplit(':', 1)
            try:
                port = int(port_str)
            except ValueError:
                print(f"Invalid port: {port_str}")
                sys.exit(1)
        else:
            host = args.connect
            port = args.port
        
        if args.gui:
            # GUI mode
            if not PYGAME_AVAILABLE:
                print("GUI mode requires pygame. Install with: pip install pygame")
                sys.exit(1)
            print(f"Connecting to {host}:{port} (GUI mode)...")
            fullscreen = not args.windowed
            client = NetworkGUIClient(host, port, player_name, fullscreen=fullscreen, password=password)
            try:
                client.run()
            except KeyboardInterrupt:
                pass
        else:
            # Terminal mode
            if IS_WINDOWS:
                print("Terminal mode is not supported on Windows.")
                print("Please use --gui flag for graphical mode.")
                sys.exit(1)
            print(f"Connecting to {host}:{port} (Terminal mode)...")
            def run_terminal_client(stdscr):
                client = NetworkTerminalClient(stdscr, host, port, player_name, password=password)
                client.run()
            try:
                curses.wrapper(run_terminal_client)
            except KeyboardInterrupt:
                pass
        sys.exit(0)
    
    # === LEGACY FILE-BASED MODE ===
    if not args.host and not args.join:
        print("Multi-User Snake Game")
        print("=" * 60)
        print("\nNETWORK MODE (recommended):")
        print("  Start server:")
        print(f"    python3 snake_game.py --server --port {DEFAULT_PORT}")
        print(f"    python3 snake_game.py --server --ip 192.168.1.100 --port {DEFAULT_PORT}")
        print("\n  Connect to server (GUI mode):")
        print(f"    python3 snake_game.py --connect localhost:{DEFAULT_PORT} --gui")
        print(f"    python3 snake_game.py --connect 192.168.1.100:{DEFAULT_PORT} --gui --name Player1")
        print("\n  Connect to server (Terminal mode):")
        print(f"    python3 snake_game.py --connect localhost:{DEFAULT_PORT}")
        print(f"    python3 snake_game.py --connect 192.168.1.100:{DEFAULT_PORT} --name Player1")
        print("\n  With password (for internet-exposed servers):")
        print(f"    python3 snake_game.py --connect HOST:{DEFAULT_PORT} --gui --password YOUR_PASSWORD")
        print("\n  In-game controls:")
        print("    Arrow keys = Move | SPACE = Bomb | G = Ghost | S = Start | R = Restart | Q = Quit")
        print("\nSECURITY:")
        print("  - Servers on 0.0.0.0 or public IPs auto-generate a password")
        print("  - Share the password with players to allow them to join")
        print("  - LAN servers (192.168.x.x, 10.x.x.x) don't require passwords")
        print("\nLEGACY MODE (same machine only):")
        print("  python3 snake_game.py --host --gui")
        print("  python3 snake_game.py --join --gui")
        print("\nGame options: --mode classic|kurve  --speed normal|fast|ultra  --no-walls")
        sys.exit(1)
    
    try:
        if args.gui:
            run_gui(args)
        else:
            run_terminal(args)
    except KeyboardInterrupt:
        pass
