#!/usr/bin/env python3
"""complexity-ceiling: per-file cyclomatic complexity budgets + over-budget report + CI exit codes.

Budget file (JSON):
    {"default": 8, "files": {"tests/*_special.py": 12}}
A budget value caps the maximum cyclomatic complexity of any single function
in the matching file. Pure stdlib. Exit codes: 0 pass, 1 over budget, 2 error.
"""
import argparse
import ast
import fnmatch
import json
import os
import sys

SPEC_VERSION = "1.0.0"

_DECISION_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
                   ast.IfExp, ast.Assert, ast.BoolOp, ast.comprehension, ast.match_case)


def function_complexity(func_node):
    """Cyclomatic complexity of one function, excluding nested function bodies."""
    score = 1

    def visit(node):
        nonlocal score
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # attributed to the nested callable itself
            if isinstance(child, _DECISION_NODES):
                score += 1
                if isinstance(child, ast.BoolOp):
                    score += len(child.values) - 1
                if isinstance(child, ast.comprehension):
                    score += len(child.ifs)
            visit(child)

    visit(func_node)
    return score


def file_complexities(path):
    """Return [(func_name, complexity), ...] for a Python source file."""
    with open(path, "r", encoding="utf-8", errors="strict") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
            parent = getattr(node, "parent", None)
            out.append((name, function_complexity(node)))
    return out


def _annotate_parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node
    return tree


def load_budget(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "default" not in data:
        raise ValueError("budget file must be an object with a 'default' key")
    if not isinstance(data["default"], int) or data["default"] < 1:
        raise ValueError("'default' budget must be a positive integer")
    files = data.get("files", {})
    if not isinstance(files, dict):
        raise ValueError("'files' must be an object mapping glob -> budget")
    return data["default"], files


def budget_for(relpath, default_budget, file_budgets):
    """Longest-glob-wins: the most specific (longest pattern) match applies."""
    best = None
    for pattern, value in file_budgets.items():
        norm = pattern.replace("\\", "/")
        if fnmatch.fnmatch(relpath, norm) and (best is None or len(norm) > best[0]):
            best = (len(norm), value)
    return best[1] if best else default_budget


def collect_python_files(paths, exclude=None):
    files = []
    for p in paths:
        if os.path.isfile(p):
            if p.endswith(".py"):
                files.append(p)
        elif os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv")]
                for n in sorted(names):
                    if n.endswith(".py"):
                        files.append(os.path.join(root, n))
    return sorted(set(files))


def check(paths, budget_path, exclude=None):
    default_budget, file_budgets = load_budget(budget_path)
    violations = []
    scanned = 0
    max_cc = 0
    for path in collect_python_files(paths, exclude):
        rel = path.replace("\\", "/")
        if exclude and any(fnmatch.fnmatch(rel, pat.replace("\\", "/")) for pat in exclude):
            continue
        scanned += 1
        budget = budget_for(rel, default_budget, file_budgets)
        try:
            for func_name, cc in file_complexities(path):
                max_cc = max(max_cc, cc)
                if cc > budget:
                    violations.append({"file": rel, "function": func_name,
                                       "complexity": cc, "budget": budget, "over_by": cc - budget})
        except SyntaxError as exc:
            violations.append({"file": rel, "function": "<parse>",
                               "complexity": -1, "budget": budget,
                               "over_by": -1, "error": "SyntaxError: %s" % exc.msg})
    report = {"budget_file": budget_path, "default_budget": default_budget,
              "files_scanned": scanned, "violations": violations, "worst": max_cc}
    return report


def write_template(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"default": 8, "files": {"legacy/*.py": 15}}, f, indent=2)
        f.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="complexity-ceiling",
                                 description="Per-file cyclomatic complexity budgets with CI exit codes")
    sub = ap.add_subparsers(dest="cmd")
    pc = sub.add_parser("check", help="check paths against a budget file")
    pc.add_argument("paths", nargs="+")
    pc.add_argument("--budget", required=True)
    pc.add_argument("--exclude", nargs="*", default=[])
    pc.add_argument("--report", default=None, help="write JSON report to file")
    pc.add_argument("--max-violations", type=int, default=0, dest="max_violations",
                    help="v0.1.1: tolerate up to N violations (CI ratchet)")
    pt = sub.add_parser("budget-template", help="write a starter budget file")
    pt.add_argument("--out", default="complexity_budget.json")

    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_help(sys.stderr)
        return 2
    try:
        if args.cmd == "budget-template":
            write_template(args.out)
            print("wrote %s" % args.out)
            return 0
        if args.cmd == "check":
            report = check(args.paths, args.budget, args.exclude)
            if args.report:
                with open(args.report, "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2)
            print("scanned=%d violations=%d worst_cc=%d"
                  % (report["files_scanned"], len(report["violations"]), report["worst"]))
            for v in report["violations"][:20]:
                print("OVER %s:%s cc=%d budget=%d" % (v["file"], v["function"], v["complexity"], v["budget"]))
            if len(report["violations"]) <= args.max_violations:
                return 0
            return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
