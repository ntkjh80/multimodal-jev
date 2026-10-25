"""소스만 ZIP으로 묶습니다. 키·업로드·실험 데이터는 포함하지 않습니다."""
import argparse
import zipfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(root.parent / "multimodal-bench.zip"))
    args = parser.parse_args()
    folders = {"bench", "web", "docs", "scripts", "tests", "workers", "examples", ".github"}
    top = {"README.md", "requirements.txt", "pyproject.toml", ".gitignore", ".env.example",
           "run.py", "start-macos.command", "start-windows.cmd"}
    target = Path(args.output)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if not path.is_file() or "__pycache__" in rel.parts or path.suffix == ".pyc":
                continue
            if (len(rel.parts) == 1 and rel.name in top) or rel.parts[0] in folders:
                archive.write(path, "multimodal-bench/" + rel.as_posix())
    print(target.resolve())


if __name__ == "__main__":
    main()
