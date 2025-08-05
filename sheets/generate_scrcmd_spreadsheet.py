#!/usr/bin/env python3
import json
import glob
import os
import xlsxwriter

# Configuration
PARAM_WRAP_THRESHOLD = 50
NAME_MAP = {'diamond_pearl':'DP','platinum':'Pt','hgss':'HGSS'}

# Load styles from JSON
with open('styles.json', 'r', encoding='utf-8') as f:
    s = json.load(f)
    # JSON keys are strings; convert to ints for indexing
    HEADER = {int(k): v for k, v in s['header'].items()}
    DATA = {int(k): v for k, v in s['data'].items()}
    CW = {int(k): v for k, v in s['widths'].items()}

# Create workbook
wb = xlsxwriter.Workbook('scrcmd_commands.xlsx')

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
            'align': 'left',  # Force left alignment
            'valign': style.get('align', {}).get('vert', 'top'),
            'border': 1,
            'border_color': 'CCCCCC'
        })

# Add parameter type bold format
param_bold = wb.add_format({
    'font_name': 'Courier New',
    'font_size': DATA[4]['font']['size'],
    'bold': True,
    'bg_color': DATA[4]['fill']['fg'][2:],
    'valign': DATA[4]['align']['vert'],
    'text_wrap': True,
    'border': 1,
    'border_color': 'CCCCCC'
})

# Add function column bold format for capitalized words
function_bold = wb.add_format({
    'font_name': 'Courier New',
    'font_size': DATA[5]['font']['size'],
    'bold': True,
    'bg_color': DATA[5]['fill']['fg'][2:],
    'valign': DATA[5]['align']['vert'],
    'text_wrap': True,
    'border': 1,
    'border_color': 'CCCCCC'
})

# Process JSON files
script_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(script_dir)
for idx, path in enumerate(sorted(glob.glob(os.path.join(parent_dir, '*_scrcmd_database.json')))):
    base = os.path.splitext(os.path.basename(path))[0].replace('_scrcmd_database','')
    name = NAME_MAP.get(base, base.upper())
    
    # Load JSON commands
    with open(path, 'r', encoding='utf-8') as f:
        js = json.load(f)['scrcmd']
    
    # Create worksheet
    ws = wb.add_worksheet(name)
    
    # Set column widths
    for col, width in CW.items():
        ws.set_column(col-1, col-1, width)
    
    # Set header row height
    ws.set_row(0, 30)  # Adjust height value as needed
    
    # Write headers - merged cells vertically
    for col, spec in HEADER.items():
        # Create merged cell format that copies header format but enables text wrapping
        merged_format = wb.add_format({
            'font_name': spec.get('font', {}).get('name', 'Courier New'),
            'font_size': spec.get('font', {}).get('size', 11),
            'bold': spec.get('font', {}).get('bold', False),
            'italic': spec.get('font', {}).get('italic', False),
            'bg_color': spec.get('fill', {}).get('fg', 'FFFFFF')[2:],
            'text_wrap': True,  # Enable text wrapping
            'align': 'center',  # Center text horizontally
            'valign': 'center', # Center text vertically
            'border': 1,
            'border_color': 'CCCCCC'
        })
        
        # Write the merged header cell
        ws.write(0, col-1, spec.get('value', ''), merged_format)
    
    # Freeze panes at D2
    ws.freeze_panes(1, 3)
    
    # Start data rows at row 1
    row = 1

    for code_hex, info in js.items():
        code = code_hex[2:].upper().zfill(4)
        params = info.get('parameter_values', [])
        joined = '; '.join(params)
        ps = joined if len(joined) <= PARAM_WRAP_THRESHOLD else ''.join(params)
        
        # Write each column
        ws.write(row, 0, code, formats['data_1'])
        ws.write(row, 1, info.get('decomp_name', ''), formats['data_2'])
        ws.write(row, 2, info.get('name', ''), formats['data_3'])
        
        # Write parameters column
        if params:
            # Store parameter names for later comparison
            param_names = []
            parts = []
            for param in params:
                if ':' in param:
                    type_part, rest = param.split(':', 1)
                    parts.extend([type_part, f":{rest}"])
                    # Extract parameter name (after colon, before semicolon)
                    param_name = rest.strip()
                    # Add both full name and individual words
                    param_names.append(param_name)
                    # Add individual words if it's a multi-word parameter
                    param_names.extend(param_name.split())
                else:
                    parts.append(param)
            
            # Remove duplicates while preserving order
            param_names = list(dict.fromkeys(param_names))
            
            rich_parts = []
            for i, part in enumerate(parts):
                if i % 2 == 0 and ':' in params[i//2]:  # Type parts
                    rich_parts.append({'text': part, 'format': param_bold})
                else:  # Rest of parameter or separator
                    rich_parts.append({'text': part + ('; ' if i < len(parts)-1 else ''), 'format': formats['data_4']})
            
            # Only use write_rich_string if we have enough parts
            if len(rich_parts) > 1:
                ws.write_rich_string(row, 3, *[item for p in rich_parts for item in [p['format'], p['text']]], formats['data_4'])
            else:
                # If only one part, use regular write
                ws.write(row, 3, rich_parts[0]['text'], rich_parts[0]['format'])
        else:
            param_names = []
            ws.write(row, 3, '', formats['data_4'])
        
        # Write description with parameter name matches in bold
        description = info.get('description', '')
        if description:
            words = description.split()
            if words:
                rich_parts = []
                i = 0
                while i < len(words):
                    current_word = words[i]
                    found_match = False
                    
                    # Try to match multi-word parameters
                    for param in sorted(param_names, key=len, reverse=True):
                        param_words = param.split()
                        if i + len(param_words) <= len(words):
                            potential_match = ' '.join(words[i:i+len(param_words)])
                            # Remove any trailing punctuation for comparison
                            clean_potential = potential_match.rstrip('.,;:!?')
                            if clean_potential == param:
                                rich_parts.append({'text': potential_match, 'format': function_bold})
                                rich_parts.append({'text': ' ', 'format': formats['data_5']})
                                i += len(param_words)
                                found_match = True
                                break
                    
                    if not found_match:
                        rich_parts.append({'text': current_word, 'format': formats['data_5']})
                        rich_parts.append({'text': ' ', 'format': formats['data_5']})
                        i += 1
                
                # Write with rich text formatting
                ws.write_rich_string(row, 4, *[item for p in rich_parts for item in [p['format'], p['text']]], formats['data_5'])
        else:
            ws.write(row, 4, '', formats['data_5'])
        
        row += 1
    
    # Generate CSV
    # csv_file = f'scrcmd_{base}.csv'
    # with open(csv_file, 'w', encoding='utf-8') as f:
    #     f.write('Code,Decomp Name,Name,Parameters,Description\n')
    #     for code_hex, info in js.items():
    #         code = code_hex[2:].upper().zfill(4)
    #         params = '; '.join(info.get('parameter_values', []))
    #         f.write(f'{code},{info.get("decomp_name","")},{info.get("name","")},{params},{info.get("description","")}\n')
    # print(f'CSV generated: {csv_file}')

wb.close()
print('Excel generated: scrcmd_commands.xlsx')