# 🐍 Multi-User Snake Game

This project was fully vibe coded for fun by Claude Opus 4.5.

A multiplayer Snake game for Linux/Windows supporting up to 10 players over the network.

## Features

- **Network Multiplayer**: Players can connect from different machines via TCP/UDP
- **Multi-User Support**: Up to 10 players can play simultaneously
- **Two Game Modes**:
  - **Classic**: Traditional snake - the snake only grows when eating food
  - **Kurve** (Achtung die Kurve style): The snake constantly grows and leaves a permanent trail
- **Weapon System**:
  - **Bomb (W)**: Collect bombs and shoot at other snakes (destroys 4 segments!)
  - **Ghost (G)**: Become invisible for 5 seconds - other players can't see you!
- **Shrinking Walls**: Battle royale mode activates when 3 or fewer players remain
  - Walls shrink every 15 seconds (3 players) or 10 seconds (2 players)
  - Bombs can destroy wall segments to create escape routes
- **Dead Snake Obstacles**: Eliminated players remain as solid obstacles on the field
- **Epic Winner Display**: FAT gold text, rankings, and medals (🥇🥈🥉) for top 3 players
- **Speed Modes**: Normal, Fast, Ultra
- **Wall Options**: Play with or without walls (wrap-around possible)
- **GUI Mode**: Graphical interface with Pygame (optional)
- **Terminal Mode**: Play directly in the Linux/Windows terminal (curses)
- **Explosion Animations**: Visual effects for bomb hits
- **Security Features**: Password authentication, rate limiting, connection limits

## Installation

### Requirements

```bash
# Install dependencies
pip install -r requirements.txt

# Or install manually
pip install pygame           # Optional: for GUI mode
```

### Single player GUI Mode

```bash
python snake_game.py --host --gui
```
### Single player console Mode (Linux only)

```bash
python snake_game.py --host
```

## Network Mode

### Starting a Server

```bash
# Local server (LAN - no password required)
python snake_game.py --server --ip 192.168.1.100 --port 5555

# Server on all interfaces (auto-generates password for security)
python snake_game.py server  --ip 0.0.0.0  --port 5555

# Server with custom password
python snake_game.py --server --ip 0.0.0.0 --port 5555 --password mySecretPassword123
```

When the server binds to `0.0.0.0` or a public IP, a password is **automatically generated** and displayed:

```
============================================================
⚠️  SERVER IS POTENTIALLY INTERNET-EXPOSED!
🔑 PASSWORD: abc123XYZ456def
   Share this password with players to allow them to join.
============================================================
```

### Connecting as a Client

```bash
# Connect without password (LAN server)
python snake_game.py --connect 192.168.1.100:5555

# Connect with password (internet server)
python snake_game.py --connect 192.168.1.100:5555 --password yourPassword123

# GUI mode
python snake_game.py --gui --connect 192.168.1.100:5555

# Windowed mode instead of fullscreen
python snake_game.py --windowed --connect 192.168.1.100:5555 --gui
```

**Note**: For servers on the local network (192.168.x.x, 10.x.x.x, etc.) no password is required.

## Controls

### Movement

| Key | Action |
|-----|--------|
| ↑ ↓ ← → | Arrow keys for direction |
| W A S D | Alternative movement keys |
| 8 4 6 2 | Numpad movement |

### Weapons & Actions

| Key | Action |
|-----|--------|
| Space / Enter | Fire bomb (destroys 4 segments!) |
| G | Activate Ghost mode (invisible for 5 sec) |
| S | Start game (host only) |

### Game Control

| Key | Action |
|-----|--------|
| S | Start game (when in lobby) |
| R | Restart game (after game over) |
| Q / ESC | Quit game |

## Command-Line Options

### Server Options

| Option | Description |
|--------|-------------|
| `--server --ip <IP> --port <PORT>` | Start server mode |
| `--mode classic\|kurve` | Game mode |
| `--speed normal\|fast\|ultra` | Game speed |
| `--no-walls` | No walls - wrap-around enabled |
| `--debug` | Enable debug output |

### Client Options

| Option | Description |
|--------|-------------|
| `--connect IP:PORT` | Connect to server |
| `--gui` | Graphical mode with Pygame |
| `--windowed` | Windowed mode instead of fullscreen |
| `--name NAME` | Set player name |
| `--debug` | Enable debug output |

## Game Elements

| Symbol | Element | Description |
|--------|---------|-------------|
| █ | Snake Head | Your snake's head |
| ▓ | Snake Body | Your snake's body segments |
| ░ | Dead Snake | Dead snake (becomes obstacle) |
| ● | Food | Eat to grow and score points |
| ◆ / W | Weapon | Collectible bomb |
| * | Bomb | Flying projectile |
| # | Wall | Impassable obstacle |

## Game Modes Explained

### Classic Mode
- Traditional snake gameplay
- Snake only grows when eating food (●)
- Goal: Eat as much as possible without dying

### Kurve Mode (Achtung die Kurve)
- Snake constantly grows and leaves a permanent trail
- All trails become obstacles
- Goal: Be the last snake alive

## Weapon System

- **Weapons spawn** randomly every 5-15 seconds (Symbol: W / 💣)
- **Collect** by running over them with your snake
- **Fire** with Space bar
- **Effect**: Bomb flies in your direction and **destroys 4 segments** on hit
- **Warning**: Can hit your own snake! Aim carefully!
- **Explosion animation** on hit (especially nice in GUI mode)
- One weapon per player at a time

## Endgame Features

### Shrinking Walls (Battle Royale)
When 3 or fewer players remain alive, the walls automatically activate and begin shrinking:
- **3 players alive**: Walls shrink every 15 seconds
- **2 players alive**: Walls shrink every 10 seconds
- **Strategic bombing**: Use bombs to destroy wall segments and create escape routes
- **Minimum arena**: Walls stop shrinking at 10x10 to prevent complete collapse
- Forces final confrontation and prevents indefinite stalemates

### Winner Celebration
When the game ends, an epic winner screen displays:
- **FAT gold text** announcing the winner (impossible to miss!)
- **Rankings**: Top 3 players with Olympic-style medals
  - 🥇 **1st Place** - Gold medal and largest text
  - 🥈 **2nd Place** - Silver medal and medium text
  - 🥉 **3rd Place** - Bronze medal and smaller text
- **Scores**: Points shown for each ranked player
- **Restart**: Press 'R' to play again with same players

## GUI Mode

The GUI mode offers:
- **Graphical sprites** for snakes, food, weapons
- **Fullscreen mode** for better gameplay experience
- **Explosion particles** for bomb hits
- **Colored snakes** with eyes
- **Smooth rendering** at 60 FPS

Requirements:
```bash
# Install pygame
pip install pygame

# X-Server must be available (Linux)
echo $DISPLAY  # Should show :0 or similar
```

## Death Conditions

Your snake dies when:
- Touching a wall (only if walls are enabled!)
- Touching itself
- Touching another snake (alive or dead)
- Touching shrinking walls (during endgame battle royale)
- Being reduced to less than 2 segments by a bomb

**Dead snakes remain as solid obstacles** - eliminated players continue to affect the game by creating maze-like obstacles on the playing field!

**Without walls (`--no-walls`)**: Snakes appear on the other side of the screen.

## Network Architecture

### Protocol Design
- **TCP**: For reliable messages (join requests, player inputs, authentication)
- **UDP**: For fast game state broadcasts (positions, scores)

## Gameplay Flow

1. **Start server**: One player starts the server with `--server --ip IP --port PORT`
2. **Players connect**: Other players connect with `--connect IP:PORT`
3. **Wait for start**: All players see the connected snakes in the lobby
4. **Start game**: Any player can press `S` to start the game
5. **Early game**: All players control their snakes simultaneously
6. **Endgame**: When 3 or fewer players remain, shrinking walls activate
7. **Victory**: Winner announced with rankings and medals for top 3 players
8. **Restart**: Press `R` to play again with same players

## Technical Details

- **Language**: Python 3.6+
- **Dependencies**: pygame (optional), windows-curses (Windows only)
- **Network**: Hybrid TCP/UDP with automatic fallback
- **Game Tick**: ~150ms
- **Max Players**: 10 concurrent players
- **Playing Field**: Dynamic size based on terminal/window dimensions

## Troubleshooting

**Connection to server failed:**
- Check if the server is running and reachable
- Check firewall settings (port must be open)
- For internet servers: Password entered correctly?

### Debug Mode

Use `--debug` flag to see detailed network and game information:
```bash
python snake_game.py --debug --connect 192.168.1.100:5555
```

This shows:
- Connection status
- UDP/TCP packet counts
- Player ID assignments
- Authentication flow

## License

None - Have fun playing! 🎮
