#!/usr/bin/env python3
"""
Generate Excel spreadsheet from v2 database files.

Supports both old format (*_scrcmd_database.json) and new v2 format (*_v2.json).
"""
import json
import glob
import os
import xlsxwriter

# Configuration
PARAM_WRAP_THRESHOLD = 50
NAME_MAP = {'diamond_pearl': 'DP', 'platinum': 'Pt', 'hgss': 'HGSS'}

# Load styles from JSON
script_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(script_dir, 'styles.json'), 'r', encoding='utf-8') as f:
    s = json.load(f)
    HEADER = {int(k): v for k, v in s['header'].items()}
    DATA = {int(k): v for k, v in s['data'].items()}
    CW = {int(k): v for k, v in s['widths'].items()}


def is_v2_format(data: dict) -> bool:
    """Check if database is in v2 format."""
    return "commands" in data and "meta" in data


def load_database(path: str) -> dict:
    """Load database and normalize to v2-like structure."""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    if is_v2_format(data):
        return data
    
    # Convert old format to v2-like structure for processing
    result = {
        "meta": {"version": "Unknown"},
        "commands": {},
        "sounds": data.get("sounds", {})
    }
    
    # Convert scrcmd
    for hex_id, info in data.get("scrcmd", {}).items():
        cmd_id = int(hex_id, 16)
        name = info.get("decomp_name", info.get("name", f"cmd_{cmd_id}"))
        
        # Build params from parameter_values
        params = []
        for pv in info.get("parameter_values", []):
            if ":" in pv:
                type_str, param_name = pv.split(":", 1)
                params.append({"type": type_str.strip(), "name": param_name.strip()})
            else:
                params.append({"type": "u16", "name": pv})
        
        result["commands"][name] = {
            "type": "script_cmd",
            "id": cmd_id,
            "legacy_name": info.get("name"),
            "description": info.get("description", ""),
            "params": params
        }
    
    # Convert movements
    for hex_id, info in data.get("movements", {}).items():
        mov_id = int(hex_id, 16)
        name = info.get("decomp_name", info.get("name", f"mov_{mov_id}"))
        
        # Handle name collision
        if name in result["commands"]:
            name = f"{name}Movement"
        
        result["commands"][name] = {
            "type": "movement",
            "id": mov_id,
            "legacy_name": info.get("name"),
            "description": info.get("description", "")
        }
    
    return result


def format_params(cmd: dict) -> tuple[str, list[dict]]:
    """
    Format parameters for display.
    
    Returns (formatted_string, list of {type, name} dicts for highlighting)
    """
    if "variants" in cmd:
        # Format variants
        parts = []
        for i, variant in enumerate(cmd["variants"]):
            var_params = variant.get("params", [])
            var_str = ", ".join(
                f"{p['type']}: {p['name']}" + (f"={p['const']}" if 'const' in p else "")
                for p in var_params
            )
            desc = variant.get("desc", "")
            parts.append(f"[{i}] {var_str}" + (f" - {desc}" if desc else ""))
        return "\n".join(parts), []
    
    params = cmd.get("params", [])
    if not params:
        return "", []
    
    formatted = "; ".join(f"{p['type']}: {p['name']}" for p in params)
    return formatted, params


def create_workbook():
    """Create and configure the workbook."""
    wb = xlsxwriter.Workbook(os.path.join(script_dir, 'scrcmd_database.xlsx'))
    
    # Create formats dict
    formats = {}
    for section in ['header', 'data']:
        for col, style in s[section].items():
            key = f'{section}_{col}'
            formats[key] = wb.add_format({
                'font_name': style.get('font', {}).get('name', 'Courier New'),
                'font_size': style.get('font', {}).get('size', 11),
                'bold': style.get('font', {}).get('bold', False),
                'italic': style.get('font', {}).get('italic', False),
                'bg_color': style.get('fill', {}).get('fg', 'FFFFFF')[2:],
                'text_wrap': style.get('align', {}).get('wrap', False),
                'align': 'left',
                'valign': style.get('align', {}).get('vert', 'top'),
                'border': 1,
                'border_color': 'CCCCCC'
            })
    
    # Add parameter type bold format
    formats['param_bold'] = wb.add_format({
        'font_name': 'Courier New',
        'font_size': DATA[4]['font']['size'],
        'bold': True,
        'bg_color': DATA[4]['fill']['fg'][2:],
        'valign': DATA[4]['align']['vert'],
        'text_wrap': True,
        'border': 1,
        'border_color': 'CCCCCC'
    })
    
    # Add function column bold format
    formats['function_bold'] = wb.add_format({
        'font_name': 'Courier New',
        'font_size': DATA[5]['font']['size'],
        'bold': True,
        'bg_color': DATA[5]['fill']['fg'][2:],
        'valign': DATA[5]['align']['vert'],
        'text_wrap': True,
        'border': 1,
        'border_color': 'CCCCCC'
    })
    
    return wb, formats


def write_header(ws, wb, header_spec: dict):
    """Write header row to worksheet."""
    ws.set_row(0, 30)
    for col, spec in header_spec.items():
        merged_format = wb.add_format({
            'font_name': spec.get('font', {}).get('name', 'Courier New'),
            'font_size': spec.get('font', {}).get('size', 11),
            'bold': spec.get('font', {}).get('bold', False),
            'italic': spec.get('font', {}).get('italic', False),
            'bg_color': spec.get('fill', {}).get('fg', 'FFFFFF')[2:],
            'text_wrap': True,
            'align': 'center',
            'valign': 'center',
            'border': 1,
            'border_color': 'CCCCCC'
        })
        ws.write(0, col - 1, spec.get('value', ''), merged_format)


def write_scrcmd_sheet(wb, formats, name: str, commands: dict):
    """Write script commands worksheet."""
    ws = wb.add_worksheet(name)
    
    # Set column widths
    for col, width in CW.items():
        ws.set_column(col - 1, col - 1, width)
    
    write_header(ws, wb, HEADER)
    ws.freeze_panes(1, 3)
    
    # Filter and sort script commands by ID
    script_cmds = [
        (cmd_name, cmd_data) 
        for cmd_name, cmd_data in commands.items()
        if cmd_data.get("type") == "script_cmd"
    ]
    script_cmds.sort(key=lambda x: x[1]["id"])
    
    row = 1
    for cmd_name, cmd in script_cmds:
        code = f"{cmd['id']:04X}"
        legacy_name = cmd.get("legacy_name", cmd_name)
        param_str, params = format_params(cmd)
        description = cmd.get("description", "")
        
        ws.write(row, 0, code, formats['data_1'])
        ws.write(row, 1, cmd_name, formats['data_2'])
        ws.write(row, 2, legacy_name, formats['data_3'])
        ws.write(row, 3, param_str, formats['data_4'])
        ws.write(row, 4, description, formats['data_5'])
        
        row += 1
    
    return ws


def write_movement_sheet(wb, formats, commands: dict):
    """Write movements worksheet (Platinum only)."""
    ws = wb.add_worksheet('Movements')
    
    movement_columns = {1: CW[1], 2: CW[2], 3: CW[3], 4: CW[5], 5: CW[5]}
    for col, width in movement_columns.items():
        ws.set_column(col - 1, col - 1, width)
    
    movement_headers = {
        1: HEADER[1],
        2: HEADER[2],
        3: HEADER[3],
        4: {'value': 'Function', 'font': HEADER[4]['font'], 'fill': HEADER[4]['fill']},
        5: {'value': 'Notes', 'font': HEADER[5]['font'], 'fill': HEADER[5]['fill']}
    }
    write_header(ws, wb, movement_headers)
    ws.freeze_panes(1, 3)
    
    # Filter and sort movements by ID
    movements = [
        (name, data)
        for name, data in commands.items()
        if data.get("type") == "movement"
    ]
    movements.sort(key=lambda x: x[1]["id"])
    
    row = 1
    for mov_name, mov in movements:
        code = f"{mov['id']:04X}"
        legacy_name = mov.get("legacy_name", mov_name)
        description = mov.get("description", "")
        
        # Split description at semicolon
        if ';' in description:
            function, notes = description.split(';', 1)
            notes = notes.strip()
        else:
            function = description
            notes = ''
        
        ws.write(row, 0, code, formats['data_1'])
        ws.write(row, 1, mov_name, formats['data_2'])
        ws.write(row, 2, legacy_name, formats['data_3'])
        ws.write(row, 3, function, formats['data_4'])
        ws.write(row, 4, notes, formats['data_5'])
        
        row += 1
    
    return ws


def write_sound_sheet(wb, formats, name: str, sounds: dict):
    """Write sounds worksheet."""
    ws = wb.add_worksheet(name)
    
    sound_columns = {1: CW[1], 2: CW[3], 3: CW[5]}
    for col, width in sound_columns.items():
        ws.set_column(col - 1, col - 1, width)
    
    sound_headers = {
        1: {'value': 'ID', 'font': HEADER[1]['font'], 'fill': HEADER[1]['fill']},
        2: {'value': 'Name', 'font': HEADER[3]['font'], 'fill': HEADER[3]['fill']},
        3: {'value': 'Used In', 'font': HEADER[5]['font'], 'fill': HEADER[5]['fill']}
    }
    write_header(ws, wb, sound_headers)
    ws.freeze_panes(1, 1)
    
    # Sort sounds by ID (handle both string and int keys)
    sound_items = []
    for sound_id, info in sounds.items():
        try:
            numeric_id = int(sound_id)
        except ValueError:
            numeric_id = 0
        sound_items.append((sound_id, numeric_id, info))
    sound_items.sort(key=lambda x: x[1])
    
    row = 1
    for sound_id, _, info in sound_items:
        ws.write(row, 0, str(sound_id), formats['data_1'])
        ws.write(row, 1, info.get('name', ''), formats['data_3'])
        ws.write(row, 2, info.get('used_in', ''), formats['data_5'])
        row += 1
    
    return ws


def main():
    parent_dir = os.path.dirname(script_dir)
    
    # Find database files - prefer v2 format
    v2_files = sorted(glob.glob(os.path.join(parent_dir, '*_v2.json')))
    old_files = sorted(glob.glob(os.path.join(parent_dir, '*_scrcmd_database.json')))
    
    # Use v2 files if available, otherwise fall back to old format
    if v2_files:
        db_files = v2_files
        print(f"Using v2 format files: {len(v2_files)} found")
    else:
        db_files = old_files
        print(f"Using old format files: {len(old_files)} found")
    
    if not db_files:
        print("No database files found!")
        return 1
    
    wb, formats = create_workbook()
    platinum_commands = None
    platinum_sounds = None
    
    for path in db_files:
        # Extract game name from filename
        base = os.path.basename(path)
        if '_v2.json' in base:
            game_key = base.replace('_v2.json', '')
        else:
            game_key = base.replace('_scrcmd_database.json', '')
        
        sheet_name = NAME_MAP.get(game_key, game_key.upper())
        print(f"Processing {base} -> {sheet_name}")
        
        data = load_database(path)
        
        # Write script commands sheet
        write_scrcmd_sheet(wb, formats, sheet_name, data["commands"])
        
        # Store Platinum data for movements sheet
        if "platinum" in game_key.lower():
            platinum_commands = data["commands"]
            platinum_sounds = data.get("sounds", {})
    
    # Write Platinum movements (only once, not per-game)
    if platinum_commands:
        write_movement_sheet(wb, formats, platinum_commands)
    
    # Write sound sheets for each game
    for path in db_files:
        base = os.path.basename(path)
        if '_v2.json' in base:
            game_key = base.replace('_v2.json', '')
        else:
            game_key = base.replace('_scrcmd_database.json', '')
        
        sheet_name = f"Sounds {NAME_MAP.get(game_key, game_key.upper())}"
        
        data = load_database(path)
        sounds = data.get("sounds", {})
        if sounds:
            write_sound_sheet(wb, formats, sheet_name, sounds)
    
    wb.close()
    print(f"Excel generated: {os.path.join(script_dir, 'scrcmd_database.xlsx')}")
    return 0


if __name__ == "__main__":
    exit(main())
