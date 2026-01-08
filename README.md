# scrcmd-database

JSON database of script commands, movements, and related data for Pokemon Generation 4 ROM hacking tools.

**Supported Games:** Diamond/Pearl, Platinum, HeartGold/SoulSilver

**Published Spreadsheet:** [Google Sheets](https://docs.google.com/spreadsheets/d/1WE6aCJeVbIMDfWYPykQEqLyBAZCDK8YlYFBD6hChiVA)

## Database Files

| File | Description |
|------|-------------|
| `*_scrcmd_database.json` | Legacy format (used by DSPRE) |
| `*_v2.json` | New normalized v2 format |
| `custom_databases/` | ROM hack-specific databases (e.g., Following Platinum) |

## Data Types

The database contains several categories of data:

- **Script Commands** - Opcodes that control game logic, dialogue, events
- **Movement Commands** - NPC/event animation opcodes (walk, turn, emote, etc.). Most have a `length` parameter with default `1`.
- **Levelscript Commands** - Map initialization triggers (on load, on transition)
- **Macros** - Convenience wrappers that expand to multiple commands
- **Sounds** - Sound effect ID mappings
- **Comparison Operators** - Conditional comparison constants
- **Special Overworlds** - Reserved overworld sprite IDs

## V2 Schema

The v2 format unifies all command types into a single `commands` dictionary:

```json
{
  "meta": {
    "version": "Platinum",
    "generated_at": "2026-01-03T00:12:27+00:00",
    "generated_from": "platinum_scrcmd_database.json"
  },
  "commands": {
    "SetFlag": {
      "type": "script_cmd",
      "id": 30,
      "legacy_name": "SetFlag",
      "description": "Sets flag to TRUE",
      "params": [{"name": "flag_id", "type": "flag"}]
    },
    "FaceNorth": {
      "type": "movement",
      "id": 0,
      "description": "Event faces up",
      "params": [{"name": "length", "type": "u16", "default": "1"}]
    },
    "GoToIfEq": {
      "type": "macro",
      "params": [
        {"name": "varID", "type": "var"},
        {"name": "value", "type": "var"},
        {"name": "offset", "type": "label"}
      ],
      "expansion": [
        "CompareVar $varID, $value",
        "GoToIf 1, $offset"
      ]
    }
  },
  "sounds": { ... },
  "comparison_operators": { ... }
}
```

### Command Types

| Type | Description |
|------|-------------|
| `script_cmd` | Regular script command with numeric opcode |
| `movement` | Movement/animation command |
| `levelscript_cmd` | Map initialization trigger |
| `macro` | Convenience macro that expands to multiple commands |

### Parameter Format
```json
{
  "params": [
    {"name": "target_flag", "type": "flag"},
    {"name": "value", "type": "u16", "default": "0"}
  ]
}
```

### Parameter Types

| Type | Description |
|------|-------------|
| `u8`, `u16`, `u32` | Unsigned integers |
| `var` | Script variable ID |
| `flag` | Flag ID |
| `label` | Script offset/label |
| `msg_id` | Message/text ID |

## Usage

### For Tool Developers

The v2 format is recommended for new tools. Key fields:

- `commands[name].id` - Numeric opcode
- `commands[name].type` - Command category
- `commands[name].params` - Parameter definitions with types
- `commands[name].expansion` - For macros, the commands they expand to

### For DSPRE Users

DSPRE uses the legacy `*_scrcmd_database.json` files directly.

## Scripts

```bash
# Compare database with pret decomp projects (read-only)
python scripts/sync_from_decomp.py --all -v

# Sync and apply all changes from decomp
python scripts/sync_from_decomp.py --all --update
```

### Sync Options

| Flag | Description |
|------|-------------|
| `--update` | Apply changes to database (names, opcodes, params, defaults, types, macros) |
| `-v` | Verbose output |

### Dependencies

The sync script uses [metang](https://github.com/lhearachel/metang) to resolve movement action constants from decomp projects. Metang is included as a git submodule:

```bash
# Initialize submodules after cloning
git submodule update --init --recursive
```

This allows the sync script to fetch `movement_actions.txt` from decomp and generate proper numeric opcodes for all movement commands.

## Data Sources

Command definitions are sourced from:
- [DSPRE](https://github.com/AdAstra-LD/DS-Pokemon-Rom-Editor) research
- [pret/pokeplatinum](https://github.com/pret/pokeplatinum) decompilation
- [pret/pokeheartgold](https://github.com/pret/pokeheartgold) decompilation
- Community contributions

## Contributing

1. Edit the `*_v2.json` files directly (V2 format is the source of truth)
2. Run sync script to verify against decomp projects: `python scripts/sync_from_decomp.py --all --update`

## License

See [LICENSE](LICENSE) file.
