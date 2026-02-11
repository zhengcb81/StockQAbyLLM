import re
import os

def fix_lazy_logging(content):
    def replacement(match):
        level = match.group(1)
        f_string_content = match.group(2)
        
        # Check if there are complex expressions that might break our simple regex
        if '[' in f_string_content or '(' in f_string_content:
            return match.group(0)
            
        parts = []
        format_str = ""
        last_pos = 0
        
        # Regex to find {expression[:format]}
        # We only match simple variables or attribute access like obj.attr
        for m in re.finditer(r'\{([a-zA-Z_][a-zA-Z0-9_\.]*)(?::([^{}]+?))?\}', f_string_content):
            format_str += f_string_content[last_pos:m.start()].replace('%', '%%')
            expr = m.group(1).strip()
            fmt = m.group(2)
            
            if fmt:
                if 'f' in fmt:
                    format_str += f"%{fmt}"
                elif 'd' in fmt:
                    format_str += f"%{fmt}"
                else:
                    format_str += "%s"
            else:
                if expr.endswith('_count') or expr.endswith('_idx') or expr == 'i' or expr == 'idx':
                    format_str += "%d"
                else:
                    format_str += "%s"
            
            parts.append(expr)
            last_pos = m.end()
            
        format_str += f_string_content[last_pos:].replace('%', '%%')
        
        if not parts or last_pos < len(f_string_content) and '{' in f_string_content[last_pos:]:
            # If we didn't match all placeholders, don't change anything
            return match.group(0)
            
        return f'logger.{level}("{format_str}", {", ".join(parts)})'

    return re.sub(r'logger\.(debug|info|warning|error|critical)\(f"(.+?)"\)', replacement, content)

# Process files in src/
for root, dirs, files in os.walk('src'):
    for file in files:
        if file.endswith('.py'):
            path = os.path.join(root, file)
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            new_content = fix_lazy_logging(content)
            
            if new_content != content:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f"Fixed {path}")