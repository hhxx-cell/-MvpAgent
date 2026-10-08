"""Build a Linux x86_64, rules-gateway bundle from locked source and Docker images.

Run on an online Linux x86_64 host with Docker. Installation from the resulting
directory does not build images or contact a registry or Python package index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ("aftersales-data-agent:local", "qdrant/qdrant:v1.13.6")
SOURCE_ITEMS = (
    ".dockerignore",
    ".env.example",
    ".gitignore",
    ".pre-commit-config.yaml",
    ".github",
    "agent",
    "api",
    "database",
    "docker",
    "docs",
    "eval",
    "knowledge",
    "mock",
    "scripts",
    "tests",
    "data_manifest.json",
    "pyproject.toml",
    "README.md",
    "uv.lock",
    "智能售后数据Agent实施文档.md",
)
SKIP_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", ".git", "runtime"}


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_filter(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    filename = Path(member.name).name
    if (
        any(part in SKIP_NAMES for part in Path(member.name).parts)
        or filename.endswith(".pyc")
        or filename == ".env"
        or filename.startswith(".env.")
        and filename != ".env.example"
    ):
        return None
    member.mtime = 0
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def build_source_archive(destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        for name in SOURCE_ITEMS:
            path = ROOT / name
            if path.exists():
                archive.add(path, arcname=name, filter=source_filter)


def image_architecture(image: str) -> str:
    return run("docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image, capture=True)


def compose_has_no_build() -> None:
    output = run(
        "docker", "compose", "-f", "docker/docker-compose.offline.yml", "config", "--format", "json", capture=True
    )
    for service_name, service in json.loads(output)["services"].items():
        if "build" in service or service.get("pull_policy") != "never":
            raise ValueError(f"offline service {service_name} may build or pull an image")


def build_bundle(destination: Path, pip_index_url: str | None = None, reuse_images: bool = False) -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise SystemExit("Build this bundle on Linux x86_64")
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit(f"Output directory must be empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    run("docker", "compose", "version")
    compose_has_no_build()

    if not reuse_images:
        run("docker", "compose", "-f", "docker/docker-compose.yml", "build", "api", "mock")
    if subprocess.run(("docker", "image", "inspect", IMAGES[1]), cwd=ROOT, capture_output=True).returncode != 0:
        run("docker", "pull", IMAGES[1])
    for image in IMAGES:
        architecture = image_architecture(image)
        if architecture != "linux/amd64":
            raise ValueError(f"{image} has architecture {architecture}, expected linux/amd64")

    run("docker", "image", "save", "-o", str(destination / "images.tar"), *IMAGES)
    build_source_archive(destination / "source.tar.gz")
    shutil.copy2(ROOT / "scripts/install_offline_bundle.sh", destination / "install.sh")

    # The prebuilt image is sufficient to start offline. Keep exact wheels as
    # an additional reconstruction aid for this Linux/Python architecture.
    (destination / "wheelhouse").mkdir()
    pip_env = ("--env", f"PIP_INDEX_URL={pip_index_url}") if pip_index_url else ()
    run(
        "docker",
        "run",
        "--rm",
        "--user",
        "0:0",
        "--volume",
        f"{destination.resolve()}:/bundle",
        *pip_env,
        "--entrypoint",
        "/bin/sh",
        IMAGES[0],
        "-c",
        "cd /app && uv export --frozen --no-dev --no-emit-project --no-hashes "
        "--format requirements.txt --output-file /bundle/requirements.txt && "
        "/usr/local/bin/python -m pip download --disable-pip-version-check "
        "--only-binary=:all: --no-deps --dest /bundle/wheelhouse "
        "-r /bundle/requirements.txt",
    )
    model_manifest = {
        "gateway": "rules",
        "model_files": [],
        "reason": "No model weights are used by the rules gateway.",
    }
    (destination / "model-manifest.json").write_text(json.dumps(model_manifest, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "platform": "linux/amd64",
        "created_at": datetime.now(UTC).isoformat(),
        "images": {
            image: run("docker", "image", "inspect", "--format", "{{.Id}}", image, capture=True) for image in IMAGES
        },
        "gateway": "rules",
    }
    (destination / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    files = sorted(path for path in destination.rglob("*") if path.is_file())
    (destination / "SHA256SUMS").write_text(
        "".join(f"{sha256(path)}  {path.relative_to(destination).as_posix()}\n" for path in files), encoding="ascii"
    )
    print(f"Offline bundle ready: {destination}")
    print("Transfer the whole directory, then run: bash install.sh")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "runtime" / f"offline-linux-amd64-{datetime.now(UTC):%Y%m%dT%H%M%SZ}",
    )
    parser.add_argument("--pip-index-url", help="Optional package index for the wheelhouse download")
    parser.add_argument("--reuse-images", action="store_true", help="Use previously built local images")
    args = parser.parse_args()
    build_bundle(args.output.resolve(), args.pip_index_url, args.reuse_images)


if __name__ == "__main__":
    main()
