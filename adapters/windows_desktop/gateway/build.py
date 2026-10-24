"""Build a local shim from source; never replace Codex or change global environment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent/"bridge"))
import bridge_config as config
from read_engine_identity import ALLOWLIST, read_engine_identity


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source,"sha256").hexdigest()


def build(identity, desktop, output, compiler):
    from native_discovery import _WindowsBackend, _eligible_node
    parent=_WindowsBackend().inspect_process(identity.get("parent_pid",0))
    if parent is None or _eligible_node(parent) is None:
        raise ValueError("engine_parent_must_be_verified_registered_desktop_in_same_session")
    if set(identity["sandbox_environment"]) != set(ALLOWLIST):
        raise ValueError("sandbox_environment_allowlist_invalid")
    real=Path(identity["executable"]).resolve(strict=True)
    local_bin=Path(os.environ["LOCALAPPDATA"])/"OpenAI/Codex/bin"
    package_cli=(Path(parent.package_path)/"app/resources/codex.exe").resolve()
    if real.name.casefold() != "codex.exe" or not (real.is_relative_to(local_bin.resolve()) or real==package_cli):
        raise ValueError("engine_must_be_installed_codex_cli")
    if sha256(real) != identity["sha256"]:
        raise ValueError("engine_changed")
    if Path(sys.executable).name.casefold() != "python.exe" or sys.version_info[:2] != (3,12):
        raise ValueError("python_312_console_required")
    if output.exists():
        raise ValueError("output_already_exists_choose_a_new_directory")
    output.mkdir(parents=True)
    settings={"real_cli":str(real),"real_cli_sha256":identity["sha256"],
              "python":str(Path(sys.executable).resolve()),"proxy":str((HERE.parent/"bridge/native_gateway_proxy.py").resolve()),
              "desktop":str(desktop),"sandbox_environment":identity["sandbox_environment"]}
    target=output/"launcher-config.json"
    target.write_text(json.dumps(settings,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    generated=output/"CodexGatewayShim.generated.cs"
    generated.write_text((HERE/"CodexGatewayShim.cs").read_text(encoding="utf-8").replace("__CONFIGURATION_SHA256__",sha256(target)),encoding="utf-8")
    executable=output/"codex-gateway.exe"
    subprocess.run([str(compiler),"/nologo","/target:exe","/platform:x64","/optimize+","/reference:System.Web.Extensions.dll","/out:"+str(executable),str(generated)],check=True,capture_output=True)
    manifest={"schema":"codex-native-gateway-build/1","config_sha256":sha256(target),"shim_sha256":sha256(executable),"desktop_sha256":sha256(desktop),"source_sha256":sha256(HERE/"CodexGatewayShim.cs"),"global_environment_changed":False}
    (output/"build-manifest.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-pid",required=True,type=int,help="PID of the existing Desktop app-server codex.exe; no credentials are read or printed")
    parser.add_argument("--output",type=Path,default=config.data_dir()/"gateway-build")
    args=parser.parse_args()
    if os.name!="nt":raise SystemExit("Windows required")
    from desktop_startup import WindowsStartupBackend
    target=WindowsStartupBackend().target()
    desktop=Path(target.install_path)/"app/ChatGPT.exe"
    compiler=Path(os.environ.get("WINDIR","C:/Windows"))/"Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():raise SystemExit("Local .NET Framework C# compiler unavailable; no download or installation attempted")
    print(json.dumps(build(read_engine_identity(args.engine_pid),desktop,args.output.resolve(),compiler),indent=2))
    print("Built only. Exit Desktop gracefully when idle before explicit activate.py; original executables remain unchanged.")


if __name__=="__main__":main()
