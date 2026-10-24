"""Verify the reviewed file manifest and common accidental secret patterns."""
import hashlib
import json
from pathlib import Path,PurePosixPath
import re
import sys

GENERATED={".git",".venv","node_modules","__pycache__","build","dist",".test-results","test-results"}
RULES={
    "telegram_token":re.compile(r"(?<![A-Za-z0-9])[0-9]{6,16}:[A-Za-z0-9_-]{30,100}"),
    "private_key":re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "api_secret":re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{35,}"),
    "github_secret":re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
}


def verify(root):
    root=Path(root).resolve()
    manifest=json.loads((root/"MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("schema")!="public-release-manifest/1":raise ValueError("Invalid manifest schema")
    findings=[];declared=set()
    for row in manifest["files"]:
        relative=PurePosixPath(row["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"] or ":" in row["path"]:
            raise ValueError("Unsafe manifest path")
        if row["path"] in declared:raise ValueError("Duplicate manifest entry")
        declared.add(row["path"])
        path=root.joinpath(*relative.parts)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            findings.append({"path":row["path"],"rule":"redirected_path"});continue
        if not path.is_file():
            findings.append({"path":row["path"],"rule":"missing_file"});continue
        content=path.read_bytes()
        if len(content)!=row["bytes"] or hashlib.sha256(content).hexdigest()!=row["sha256"]:
            findings.append({"path":row["path"],"rule":"content_changed"})
        try:text=content.decode("utf-8")
        except UnicodeDecodeError:
            findings.append({"path":row["path"],"rule":"unexpected_binary"});continue
        for rule,pattern in RULES.items():
            for match in pattern.finditer(text):
                findings.append({"path":row["path"],"line":text.count("\n",0,match.start())+1,"rule":rule})
    for path in root.rglob("*"):
        relative=path.relative_to(root)
        if any(part in GENERATED or part.endswith(".egg-info") for part in relative.parts):continue
        if path.is_file() and relative.as_posix() not in declared|{"MANIFEST.json"}:
            findings.append({"path":relative.as_posix(),"rule":"unreviewed_file"})
    return manifest,findings


def main():
    try:
        manifest,findings=verify(Path(__file__).resolve().parents[1])
        print(json.dumps({"reviewed_files":len(manifest["files"]),"findings":findings,"passed":not findings},ensure_ascii=False,indent=2))
        return 1 if findings else 0
    except (OSError,ValueError,KeyError,TypeError):
        print("Release verification failed: missing or invalid manifest.")
        return 2


if __name__=="__main__":raise SystemExit(main())
