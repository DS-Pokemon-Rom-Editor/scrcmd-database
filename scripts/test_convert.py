import re

def convert_expansion_directives(expansion_lines):
    converted = []
    i = 0
    while i < len(expansion_lines):
        line = expansion_lines[i]
        
        # Check if this is a directive with a macro comment
        comment_match = re.search(r'/\*\s*(\w+)\s*\*/', line)
        if comment_match and ('.short ' in line or '.byte ' in line):
            macro_name = comment_match.group(1)
            
            # Collect subsequent .short/.byte lines with param references
            param_lines = []
            i += 1
            while i < len(expansion_lines):
                next_line = expansion_lines[i]
                
                if next_line.strip().startswith('.short ') or next_line.strip().startswith('.byte '):
                    # Keep this .short/.byte line only if it has a $param
                    if '$' in next_line:
                        param_lines.append(next_line.strip())
                        i += 1
                    else:
                        # This is a new directive without params, stop collecting
                        break
                else:
                    # Not a .short/.byte line, stop collecting
                    break
            
            # Combine macro name with params
            if param_lines:
                result = macro_name + ' ' + ' '.join(param_lines)
                converted.append(result)
            else:
                converted.append(macro_name)
            continue
        
        # Keep as-is if not a directive or no conversion
        if line.strip():
            converted.append(line.strip())
        i += 1
    
    return converted

# Test with actual format from parse_macro_conditionals
lines = [
    '    .short 40 /* SetVarFromValue */',
    '    .short \\\\$destVarID',
    '    .short \\\\$valueOrVarID'
]

print('Input:')
for l in lines:
    print(f'  {l}')

result = convert_expansion_directives(lines)
print()
print('Output:')
for r in result:
    print(f'  {r}')
