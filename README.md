# scrcmd-database

JSON database of script commands, movements, and related data for Pokemon Generation 4 ROM hacking tools.

**Supported Games:** Diamond/Pearl, Platinum, HeartGold/SoulSilver

**[View the data in Google Sheets](https://docs.google.com/spreadsheets/d/1WE6aCJeVbIMDfWYPykQEqLyBAZCDK8YlYFBD6hChiVA)**

**V2 format is the source of truth. Legacy files are maintained for compatibility.**

## Database Files

| File | Description |
|------|-------------|
| `*_v2.json` | Primary format - Modern, unified schema |
| `*_scrcmd_database.json` | Legacy format (DSPRE compatibility) |
| `custom_databases/` | ROM hack-specific databases (e.g., Following Platinum) |

## Data Types

The database contains several categories of data:

- **Script Commands** - Opcodes that control game logic, dialogue, events
- **Movement Commands** - NPC/event animation opcodes (walk, turn, emote, etc.). Most have a `length` parameter with default `1`.

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
    "Message": {
      "type": "script_cmd",
      "id": 47,
      "description": "Shows message box",
      "variants": {
        "0": {
          "description": "Default message",
          "params": [{"name": "msg_id", "type": "msg_id"}]
        },
        "1": {
          "description": "Message with speaker",
          "params": [
            {"name": "msg_id", "type": "msg_id"},
            {"name": "speaker_id", "type": "u16"}
          ]
        }
      }
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

### Conditional Commands (Variants)
Some commands have different parameter sets based on a mode parameter:
```json
{
  "variants": {
    "0": {
      "description": "Mode 0 behavior",
      "params": [{"name": "param1", "type": "u8"}]
    },
    "1": {
      "description": "Mode 1 behavior", 
      "params": [
        {"name": "param1", "type": "u8"},
        {"name": "param2", "type": "u16"}
      ]
    }
  }
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

DSPRE currently uses the legacy `*_scrcmd_database.json` files. Support for the v2 format is planned for integration with the custom script compiler.

## Scripts

```bash
# Regenerate every v2 database from the legacy DSPRE-format JSON files
python scripts/db_migration.py

# Import decomp-derived names, params, movements, and macros into every v2 database
python scripts/sync_from_decomp.py
```

`db_migration.py` rewrites every `*_v2.json` file next to its matching
`*_scrcmd_database.json` source, including custom databases under `custom_databases/`.

`sync_from_decomp.py` then enriches every v2 database with data from the supported
decomp projects. Diamond/Pearl is skipped automatically because there is no configured
decomp source yet.

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
2. Run `python scripts/sync_from_decomp.py` to refresh decomp-derived metadata

## License

See [LICENSE](LICENSE) file.
