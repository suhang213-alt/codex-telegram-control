"""Archive only reviewed files. No glob-copy of a user's working directory."""
import argparse
import hashlib
from pathlib import Path
import sys
import tomllib
import zipfile
from verify_release import verify


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",required=True,help="Destination ZIP outside this repository")
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    version=tomllib.loads((root/"pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    import re
    if not isinstance(version,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}",version):
        parser.error("Unsafe release version")
    destination=Path(args.output).expanduser().resolve()
    if destination.is_relative_to(root) or destination.suffix.lower()!=".zip" or destination.exists():
        parser.error("Use a new .zip path outside the repository")
    manifest,findings=verify(root)
    if findings:
        print("Release blocked: verify_release.py reported findings.")
        return 1
    destination.parent.mkdir(parents=True,exist_ok=True)
    files=[x["path"] for x in manifest["files"]]+["MANIFEST.json"]
    with zipfile.ZipFile(destination,"x",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for relative in sorted(files):
            info=zipfile.ZipInfo("codex-telegram-control-"+version+"/"+relative,date_time=(2026,10,24,0,0,0))
            info.create_system=3
            mode=0o100755 if relative.endswith((".sh",".command")) else 0o100644
            info.external_attr=mode<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,(root/relative).read_bytes(),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    with zipfile.ZipFile(destination) as archive:
        bad=archive.testzip()
        if bad:raise ValueError("Archive CRC failed")
    digest=hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(".zip.sha256").write_text(digest+"  "+destination.name+"\n",encoding="ascii")
    print(f"Archive verified: {len(files)} files; SHA-256 {digest}")
    return 0


if __name__=="__main__":raise SystemExit(main())
