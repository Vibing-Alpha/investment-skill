---
globs: ["scripts/**", "*.py", "*.sh"]
---

## Development Rules

### Cross-Platform Compatibility

All scripts must work on Linux, macOS, and Windows (WSL/native).

- Use `pathlib.Path` instead of hardcoded `/` path separators
- Use `sys.executable` instead of hardcoded `python3`
- Avoid shell-specific syntax in Python scripts (no bash-isms)
- Use `subprocess.run()` with list args, not shell=True with string commands
- Use `os.environ` for environment variables, not shell expansion
- Test file paths: no assumptions about case sensitivity
- Use `shutil` for file operations, not os.system("cp"/"mv")

### Script Standards

- Entry point via `if __name__ == "__main__"` with `sys.argv` or `argparse`
- Exit codes: 0=success, 1=failure, 2=error
- All output to stdout (data) or stderr (logs), never mixed
- UTF-8 encoding explicit on all file I/O: `open(f, encoding="utf-8")`
