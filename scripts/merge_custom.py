#!/usr/bin/env python3
"""
Merge script for combining base databases with custom deltas.

Delta format:
{
  "meta": {
    "base": "platinum_v2.json",
    "variant": "following_platinum",
    "description": "Following Platinum romhack additions and modifications"
  },
  "add": {
    "CommandName": { full command object }
  },
  "modify": {
    "CommandName": { fields to override/merge }
  },
  "remove": ["CommandName1", "CommandName2"]
}
"""
import json
import os
import argparse
import copy
from datetime import datetime, timezone


def generate_delta(base_path: str, custom_path: str, delta_path: str) -> None:
    """Generate a delta file by comparing base and custom databases."""
    with open(base_path, 'r', encoding='utf-8') as f:
        base = json.load(f)
    with open(custom_path, 'r', encoding='utf-8') as f:
        custom = json.load(f)
    
    base_cmds = base.get("commands", {})
    custom_cmds = custom.get("commands", {})
    
    delta = {
        "meta": {
            "base": os.path.basename(base_path),
            "variant": os.path.splitext(os.path.basename(delta_path))[0],
            "generated_at": datetime.now(timezone.utc).isoformat()
        },
        "add": {},
        "modify": {},
        "remove": []
    }
    
    # Find additions (in custom but not base)
    for name, cmd in custom_cmds.items():
        if name not in base_cmds:
            delta["add"][name] = cmd
    
    # Find removals (in base but not custom)
    for name in base_cmds:
        if name not in custom_cmds:
            delta["remove"].append(name)
    
    # Find modifications (same key, different content)
    for name in set(base_cmds.keys()) & set(custom_cmds.keys()):
        if base_cmds[name] != custom_cmds[name]:
            # Store the full replacement for simplicity
            # Could be optimized to store only changed fields
            delta["modify"][name] = custom_cmds[name]
    
    # Clean up empty sections
    if not delta["add"]:
        del delta["add"]
    if not delta["modify"]:
        del delta["modify"]
    if not delta["remove"]:
        del delta["remove"]
    
    with open(delta_path, 'w', encoding='utf-8') as f:
        json.dump(delta, f, indent=2)
    
    stats = []
    if "add" in delta:
        stats.append(f"{len(delta['add'])} added")
    if "modify" in delta:
        stats.append(f"{len(delta['modify'])} modified")
    if "remove" in delta:
        stats.append(f"{len(delta['remove'])} removed")
    
    print(f"Generated delta: {delta_path}")
    print(f"  {', '.join(stats)}")


def merge_delta(base_path: str, delta_path: str, output_path: str) -> None:
    """Merge a base database with a delta to produce a full custom database."""
    with open(base_path, 'r', encoding='utf-8') as f:
        base = json.load(f)
    with open(delta_path, 'r', encoding='utf-8') as f:
        delta = json.load(f)
    
    # Deep copy base to avoid modifying original
    output = copy.deepcopy(base)
    
    # Update meta
    output["meta"]["variant"] = delta.get("meta", {}).get("variant", "custom")
    output["meta"]["generated_at"] = datetime.now(timezone.utc).isoformat()
    output["meta"]["base_file"] = delta.get("meta", {}).get("base", os.path.basename(base_path))
    
    commands = output.get("commands", {})
    
    # Apply removals first
    for name in delta.get("remove", []):
        if name in commands:
            del commands[name]
    
    # Apply modifications (full replacement)
    for name, cmd in delta.get("modify", {}).items():
        commands[name] = cmd
    
    # Apply additions
    for name, cmd in delta.get("add", {}).items():
        commands[name] = cmd
    
    output["commands"] = commands
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2)
    
    print(f"Merged to: {output_path}")
    
    # Count by type
    type_counts = {}
    for cmd in commands.values():
        t = cmd.get("type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    
    for t, count in sorted(type_counts.items()):
        print(f"  {t}: {count}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate or merge custom database deltas"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Generate delta subcommand
    gen_parser = subparsers.add_parser(
        "generate",
        help="Generate a delta by comparing base and custom databases"
    )
    gen_parser.add_argument("base", help="Base database file (e.g., platinum_v2.json)")
    gen_parser.add_argument("custom", help="Custom database file to diff against base")
    gen_parser.add_argument("-o", "--output", required=True, help="Output delta file")
    
    # Merge subcommand
    merge_parser = subparsers.add_parser(
        "merge",
        help="Merge a base database with a delta"
    )
    merge_parser.add_argument("base", help="Base database file")
    merge_parser.add_argument("delta", help="Delta file to apply")
    merge_parser.add_argument("-o", "--output", required=True, help="Output merged file")
    
    # Merge-all subcommand (for CI)
    merge_all_parser = subparsers.add_parser(
        "merge-all",
        help="Merge all deltas in custom_databases/deltas/"
    )
    merge_all_parser.add_argument(
        "--base-dir",
        default=".",
        help="Directory containing base database files"
    )
    merge_all_parser.add_argument(
        "--delta-dir",
        default="custom_databases/deltas",
        help="Directory containing delta files"
    )
    merge_all_parser.add_argument(
        "--output-dir",
        default="custom_databases",
        help="Directory for output merged files"
    )
    
    args = parser.parse_args()
    
    if args.command == "generate":
        generate_delta(args.base, args.custom, args.output)
    
    elif args.command == "merge":
        merge_delta(args.base, args.delta, args.output)
    
    elif args.command == "merge-all":
        import glob
        
        delta_files = glob.glob(os.path.join(args.delta_dir, "*.json"))
        if not delta_files:
            print(f"No delta files found in {args.delta_dir}")
            return 1
        
        for delta_path in delta_files:
            with open(delta_path, 'r', encoding='utf-8') as f:
                delta = json.load(f)
            
            base_name = delta.get("meta", {}).get("base")
            if not base_name:
                print(f"Warning: {delta_path} has no meta.base, skipping")
                continue
            
            base_path = os.path.join(args.base_dir, base_name)
            if not os.path.exists(base_path):
                print(f"Warning: Base file {base_path} not found, skipping")
                continue
            
            variant = delta.get("meta", {}).get("variant", "custom")
            output_name = f"{variant}_v2.json"
            output_path = os.path.join(args.output_dir, output_name)
            
            merge_delta(base_path, delta_path, output_path)
            print()
    
    return 0


if __name__ == "__main__":
    exit(main())
